# Copyright (c) Meta Platforms, Inc. and affiliates
import argparse
import os
import unittest

import torch
import torch.distributed as dist

from pippy.fx.passes import shape_prop
from pippy.IR import MultiUseParameterConfig, Pipe, pipe_split


d_hid = 512
bs = 256

torch.manual_seed(0)


class ExampleCode(torch.nn.Module):
    def __init__(self):
        super().__init__()
        self.mm_param = torch.nn.Parameter(torch.randn(d_hid, d_hid))
        self.mm_param2 = torch.nn.Parameter(torch.randn(d_hid, d_hid))
        self.lin = torch.nn.Linear(d_hid, d_hid)

    def forward(self, x):
        x = torch.mm(x, self.mm_param)
        #skip_connection = x
        x = torch.relu(x)
        pipe_split()
        x = torch.mm(x, self.mm_param)
        x = self.lin(x)
        pipe_split()
        x = torch.relu(x)
        #x = x + skip_connection
        x = torch.mm(x, self.mm_param2)
        pipe_split()
        x = self.lin(x)
        x = torch.relu(x)
        return x


def run_worker(args):
    MULTI_USE_PARAM_CONFIG = (
        MultiUseParameterConfig.REPLICATE
        if args.replicate
        else MultiUseParameterConfig.TRANSMIT
    )
    if args.rank == 0:
        print(f"REPLICATE config: {args.replicate} -> {MULTI_USE_PARAM_CONFIG}")

    ec = ExampleCode()
    ec.to(args.device)
    ec_input = torch.randn(bs, d_hid, device=args.device)

    ec_pipe = Pipe.from_tracing(ec, MULTI_USE_PARAM_CONFIG)

    gm = ec_pipe.split_gm
    if args.rank == 0:
        print(gm)
        gm.graph.print_tabular()

    sp = shape_prop.ShapeProp(gm)
    sp.propagate(ec_input)

    for i, (name, submod) in enumerate(gm.named_children()):
        if args.rank == i:
            break
    print(name)
    print(submod)

    for node in gm.graph.nodes:
        if node.name == name:
            input = node.args[0]  # args is a tuple
            break
    tensor_meta = input.meta["tensor_meta"]
    print(tensor_meta)

    if args.rank == 0:
        x = ec_input
    else:
        x = torch.empty(tensor_meta.shape, dtype=tensor_meta.dtype, device = args.device)
        dist.recv(x, args.rank - 1)

    y = submod(x)

    dist.send(y, (args.rank + 1) % args.world_size)

    if args.rank == 0:
        # Assuming size does not change
        dist.recv(y, args.world_size - 1)
        ref_out = ec_pipe(ec_input)
        torch.testing.assert_close(y, ref_out)
        print(
            f'equivalence test passed {torch.sum(y)} ref {torch.sum(ref_out)}'
        )


def main(args=None):
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--world_size", type=int, default=int(os.getenv("WORLD_SIZE", 4))
    )
    parser.add_argument("--rank", type=int, default=int(os.getenv("RANK", -1)))
    parser.add_argument(
        "--master_addr", type=str, default=os.getenv("MASTER_ADDR", "localhost")
    )
    parser.add_argument(
        "--master_port", type=str, default=os.getenv("MASTER_PORT", "29500")
    )
    parser.add_argument(
        "--replicate", type=int, default=int(os.getenv("REPLICATE", "0"))
    )
    parser.add_argument(
        "--cuda", type=int, default=int(torch.cuda.is_available())
    )
    parser.add_argument(
        "--record_mem_dumps", type=int, default=0, choices=[0, 1]
    )
    parser.add_argument("--checkpoint", type=int, default=0, choices=[0, 1])
    args = parser.parse_args(args)

    if args.cuda:
        n_devs = torch.cuda.device_count()
        dev_id = args.rank % n_devs
        args.device = f"cuda:{dev_id}"
    else:
        args.device = "cpu"

    # Init DDP process group
    backend = "nccl" if args.cuda else "gloo"
    torch.distributed.init_process_group(
        backend=backend, rank=args.rank, world_size=args.world_size,
    )

    run_worker(args)


if __name__ == "__main__":
    main()


class LocalTestC10DTest(unittest.TestCase):
    def test_forward(self):
        import random

        port = random.randint(29500, 30000)
        args = [
            "--master_port",
            str(port),
        ]
        main(args)