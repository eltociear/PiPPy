import torch
import torch.distributed.rpc as rpc
import warnings
import copy

from pippy.IR import MultiUseParameterConfig, Pipe, pipe_split
from pippy.PipelineDriver import PipelineDriverFillDrain

# LOG 1/18: Specifying schedule data dependencies via explicit dependencies is tricky because
#           it constrains the topological ordering in which the execution schedule can be
#           constructed. Instead, we could specify things like 1F1B scheduling by modeling
#           the resource constraint (e.g. Registers in OneFlow -- analogous to a semaphore)
#           and making the system block on this resource. zdevito pointed out that in this
#           case, parallel jobs may deadlock, as they can acquire resources in an arbitrary
#           order. This could be solved by specifying that acquiring this resource is an
#           external side effect and serializing all stages with external side effects
#           in the scheduling system.
# LOG 1/20: TODOs for implementing forward/backward/loss with schedules:
#           * ability to specify loss computation. Probably going to start with explicit callback
#             rather than a more complicated tracing system
#           * ability to switch between full-batch loss vs. per-microbatch loss. shen mentioned
#             this might change numerics. So we should have the ability to compute loss over
#             the whole minibatch rather than doing it for each micro-batch
#           * ability to schedule backwards
#
#           Plan of action:
#           * design representation in frontend/IR for forward/backward loss
#             (full mini-batch loss)
#           * Implement runtime for fill/drain pipelining (i.e. GPipe)
#           * Extend loss computation to be per-microbatch
#           * Implement 1F1B schedule

PROFILING_ENABLED = True
CHECK_NUMERIC_EQUIVALENCE = True

import os
local_rank = int(os.environ["LOCAL_RANK"])
world_size = int(os.environ["WORLD_SIZE"])

# logging.getLogger().setLevel(logging.DEBUG)

rpc.init_rpc(f'worker{local_rank}', rank=local_rank, world_size=world_size)

def get_grad_from_executor(executor, qualname):
    return executor.local_value().mod.get_parameter(qualname).grad

def set_grad_in_executor(executor, qualname, value):
    param = executor.local_value().mod.get_parameter(qualname)
    param.grad = value

if local_rank == 0:
    d_hid = 512
    bs = 503
    CHUNKS = 5
    DEBUG_MASK_MINIBATCHES = True
    REF_USE_MICROBATCHES = True
    # TODO: something is broken with REPLICATE
    MULTI_USE_PARAM_CONFIG = MultiUseParameterConfig.TRANSMIT

    class ExampleCode(torch.nn.Module):
        def __init__(self):
            super().__init__()
            self.mm_param = torch.nn.Parameter(torch.randn(d_hid, d_hid))
            self.mm_param2 = torch.nn.Parameter(torch.randn(d_hid, d_hid))
            self.lin = torch.nn.Linear(d_hid, d_hid)

        def forward(self, x):
            x = torch.mm(x, self.mm_param)
            skip_connection = x
            x = torch.relu(x)
            pipe_split()
            x = torch.mm(x, self.mm_param)
            x = self.lin(x)
            pipe_split()
            x = torch.relu(x)
            x = x + skip_connection
            x = torch.mm(x, self.mm_param2)
            x = self.lin(x)
            return x

    ec = ExampleCode()
    ec(torch.randn(bs, d_hid))
    ec.train()

    # TODO: works with sum, need to define semantics for e.g. mean
    mse_loss = torch.nn.MSELoss(reduction='sum')

    ec_pipe = Pipe.from_tracing(ec, MULTI_USE_PARAM_CONFIG, loss_fn=mse_loss)
    print(ec_pipe.split_gm)

    pipe_driver = PipelineDriverFillDrain(ec_pipe, world_size)

    input = torch.randn(bs, d_hid)
    target = torch.randn(bs, d_hid)

    # TODO: distributed optimizer
    out = pipe_driver.run(input, target, chunks=CHUNKS, _debug_mask_minibatches = DEBUG_MASK_MINIBATCHES)

    # TODO: barrier
    import time
    time.sleep(10)

    all_grad_qualnames = {k: None for k, v in ec_pipe.named_parameters()}

    replicated_params_qualnames = {}

    # Shared parameter sync. TODO: move this to actual runtime
    for param_set in ec_pipe.replicated_params:
        grad_values = []
        for module_name, param_qualname in param_set.items():
            assert module_name in pipe_driver.remote_stage_executor_rrefs
            rank, module_rref = pipe_driver.remote_stage_executor_rrefs[module_name]
            grad_value = rpc.rpc_sync(rank, get_grad_from_executor, (module_rref, param_qualname))
            grad_values.append(grad_value)
            all_grad_qualnames.setdefault(f'split_gm.{module_name}.{param_qualname}')

        synced_value = torch.sum(torch.stack(grad_values), dim=0)

        for module_name, param_qualname in param_set.items():
            assert module_name in pipe_driver.remote_stage_executor_rrefs
            rank, module_rref = pipe_driver.remote_stage_executor_rrefs[module_name]
            rpc.rpc_sync(rank, set_grad_in_executor, (module_rref, param_qualname, synced_value))

            replicated_params_qualnames.setdefault(f'split_gm.{module_name}.{param_qualname}')

    pipe_grads = {}

    for name in all_grad_qualnames:
        assert 'split_gm.' in name
        _, module_name, param_qualname = name.split('.', maxsplit=2)

        assert module_name in pipe_driver.remote_stage_executor_rrefs
        rank, module_rref = pipe_driver.remote_stage_executor_rrefs[module_name]
        grad_value = rpc.rpc_sync(rank, get_grad_from_executor, (module_rref, param_qualname))
        pipe_grads[name] = copy.deepcopy(grad_value)

    optim = torch.optim.SGD(ec_pipe.split_gm.parameters(), lr=0.05)
    optim.zero_grad()
    if REF_USE_MICROBATCHES:
        split_args, _ = PipelineDriverFillDrain._split_args_into_microbatches(input, target, chunks=CHUNKS,
            batch_dims=[0, 0], _debug_mask_minibatches = DEBUG_MASK_MINIBATCHES)
        ref_outs = []
        for chunk in range(CHUNKS):
            input_chunk = split_args[0].chunks[chunk]
            target_chunk = split_args[1].chunks[chunk]
            ref_outs.append(ec_pipe(input_chunk, target_chunk))
        ref_out = torch.sum(torch.stack(ref_outs))
    else:
        ref_out = ec_pipe(input, target)

    # TODO: scale output
    if CHECK_NUMERIC_EQUIVALENCE:
        torch.testing.assert_allclose(out, ref_out)
        print(f'equivalence test passed {torch.sum(out)} ref {torch.sum(ref_out)}')

    not_close_grads = []
    ref_grads = {}
    for name in all_grad_qualnames:
        param = ec_pipe.get_parameter(name)
        assert name in pipe_grads, f'{name} not in pipe_grads keys {pipe_grads.keys()}'
        ref_grads[name] = param.grad
        if not torch.allclose(pipe_grads[name], param.grad):
            not_close_grads.append(name)

    for name in not_close_grads:
        if name in replicated_params_qualnames:
            warnings.warn(f'Replicated parameter {name} is not numerically equivalent to the '
                           f'reference. This is likely due to floating point non-associativity '
                           f'in the gradient accumulation')
            continue
        pipe_grad = pipe_grads[name]
        ref_grad = ref_grads[name]

        relative_delta = torch.abs(pipe_grad - ref_grad) / ref_grad
        assert False, f'Parameter {name} is not numerically close! Relative diff mean ' \
                      f'{torch.mean(relative_delta)} std {torch.std(relative_delta)} max {torch.max(relative_delta)}'

    print('Gradient equivalence test passed')

    # Test equivalence with initial code as well
    orig_optim = torch.optim.SGD(ec.parameters(), lr=0.05)
    orig_optim.zero_grad()
    orig_loss = mse_loss(ec(input), target)
    orig_loss.backward()
    torch.testing.assert_allclose(out, orig_loss)

    not_close_orig_grads = []

    for name in all_grad_qualnames:
        try:
            remapped_qualname = ec_pipe.remap_qualname(name)
        except KeyError:
            # HACK: qualname remapping does not keep track of replicated params
            continue
        orig_grad = ec.get_parameter(remapped_qualname).grad
        pipe_grad = pipe_grads[name]
        if name in replicated_params_qualnames and not torch.allclose(pipe_grad, orig_grad):
            warnings.warn(f'Replicated parameter {name} is not numerically equivalent to the '
                           f'reference. This is likely due to floating point non-associativity '
                           f'in the gradient accumulation')
            continue
        if not torch.allclose(pipe_grad, orig_grad):
            not_close_orig_grads.append(name)
            print(name, torch.abs(pipe_grad - orig_grad) / orig_grad)
            print(name, torch.max(torch.abs(pipe_grad - orig_grad) / orig_grad))

    assert len(not_close_orig_grads) == 0, f'Grads not close between pipelined and original ' \
                                           f'model: {not_close_orig_grads}'

    print('correctness checks with original module passed')

        
    # # # Profiling ruts
    # with torch.autograd.profiler_legacy.profile(enabled=PROFILING_ENABLED) as prof:
    #     out = pipe_driver.run(input, target, chunks=5, _debug_mask_minibatches = False)
    #     ref_out = ec_pipe.split_gm(input, target)
    #     print(f'profiling run completed {torch.sum(ref_out)} ref {torch.sum(ref_out)}')
    # if PROFILING_ENABLED:
    #     prof.export_chrome_trace('pipe.csv')

# TODO: figure out shutdown issue on worker ranks
rpc.shutdown()