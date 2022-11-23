## PiPPY Inference For Large Models

PiPPY helps to run very large models for inference by splitting the model into mutliple stages running in multiple GPUs.
PiPPY make this easier by providing a auto split API that automates this process for user. 

### How it works
PiPPY splits your model into multiple stages, each stage loaded on one gpu then the input batch will be furhter divided into micro-batches and run through the splits from 
rank0 to the last rank. Results are being returned to rank0 as its runing the PipelineDriver.
Please read more here [Link to the main readme]

### PiPPY support arbitary checkpoint splitting 
Unlike most of the available solutions that they need to know the model architecture beforehand, PiPPY supports arbitary PyTorch checkpoints.
* PiPPY supports both manual splitting and auto split.
* Auto split support both equal_size and threshod policies.
* PiPPY use FX to trace and split the model.

### How to Use PiPPY for inference

**Define a function such as run_master() and add the followings to it.**

We use a HuggingFace T5 model as the running example here.

* Load your model normally on CPU

example:

` t5 = AutoModelForSeq2SeqLM.from_pretrained('t5-11b', use_cache=False) `

* Setup configs

 `MULTI_USE_PARAM_CONFIG = MultiUseParameterConfig.REPLICATE if args.replicate else MultiUseParameterConfig.TRANSMIT`

*  Setup the model split policy

```
if args.auto_split == "threshold":
        split_policy = split_on_size_threshold(490 * 1e6)
elif args.auto_split == "equal_size":
        split_policy = split_into_equal_size(number_of_workers)
```
* make the concerete args
```

sig = inspect.signature(t5.forward)
concrete_args = {p.name: p.default for p in sig.parameters.values() if p.name not in input_names}

```

* Split the model into a pipline, `Pipe.from_tracing` uses `torch.fx` symbolic tracing to turn our model into a directed acyclic graph (DAG) representation. Then, it groups together the operations and parameters into _pipeline stages_. Stages are represented as `submod_N` submodules, where `N` is a natural number.

```
from PiPPY.IR import Pipe

t5_pipe = Pipe.from_tracing(t5, MULTI_USE_PARAM_CONFIG, tracer=PiPPYHFTracer(), concrete_args=concrete_args,
                                output_loss_value_spec=None, split_policy=split_policy
                                )
```

* Set the number of chunks that decide the microbatch sizes

```
all_worker_ranks = pp_ranks[PiPPY.utils.exclude_master:PiPPY.utils.exclude_master + number_of_workers]
chunks = args.chunks or len(all_worker_ranks)
```
* Stream load to device using defer_stage_init, which basically let each rank trace the model and split the model and only materialize its own shard
The barrier would make sure all the rank have loaded their shards and finally we make sure that only rank0 run the pipe.

```
 t5_pipe.defer_stage_init(args.device)
 PiPPY.utils.pp_group_barrier()
 if args.rank!=0:
        return 
 ```

* Define the input/ouput to the model, "TensorChunkSpec(0)" below shows the batch dimension in the input is zero. Pipelining relies on _micro-batching_--that is--the process of dividing the program's input data into smaller chunks and feeding those chunks through the pipeline sequentially. Doing this requires that the data and operations be _separable_, i.e.there should be at least one dimension along which data can be split such that the program does not have interactions across this dimension.

```
kwargs_chunk_spec = {'input_ids': TensorChunkSpec(0), 'decoder_input_ids': TensorChunkSpec(0)}

output_chunk_spec = {"logits": TensorChunkSpec(0),"encoder_last_hidden_state": TensorChunkSpec(0)}

```
* Choose an schedule for the pipline, we use "PipelineDriverFillDrain" here, please learn more about it here.[link to pipeline scheduling]
```
schedules = {
    'FillDrain': PipelineDriverFillDrain,
    '1F1B': PipelineDriver1F1B,
    'Interleaved1F1B': PipelineDriverInterleaved1F1B,
}
```
* Now we have all the settings lets define the PipelineDriver that runs the pipeline. To learn more about different schedules for piplelines please 
```
pipe_driver: PipelineDriverBase = schedules[args.schedule](t5_pipe, chunks, args_chunk_spec, kwargs_chunk_spec,
                                                            output_chunk_spec,
                                                            world_size=len(all_worker_ranks),
                                                            all_ranks=all_worker_ranks,
                                                            _debug_mask_minibatches=False,
                                                            _record_mem_dumps=bool(args.record_mem_dumps),
                                                            checkpoint=bool(args.checkpoint),
                                                            )
```

* Run the inference by passing input data to the PipelineDriver.

`pipe_driver(**t5_input_dict)`


**we need to pass the run_master() function to the run_PiPPY() along with args to run the pipeline**

* Here we need to make sure args.gspmd is set that will let run_PiPPY() to let each rank do the trace and sharding.

```

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument('--world_size', type=int, default=int(os.getenv("WORLD_SIZE", 8)))
    parser.add_argument('--rank', type=int, default=int(os.getenv("RANK", -1)))
    parser.add_argument('--master_addr', type=str, default=os.getenv('MASTER_ADDR', 'localhost'))
    parser.add_argument('--master_port', type=str, default=os.getenv('MASTER_PORT', '29500'))
    args.gspmd = 1
    run_pippy(run_master, args)

```
Then simply run your python inference script

` python t5_inference.py`