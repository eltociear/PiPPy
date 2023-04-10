# Copyright (c) Meta Platforms, Inc. and affiliates
import argparse
import os
import logging

import torch
import pippy
import pippy.fx
from pippy import run_pippy
from pippy.hf import PiPPyHFTracer, inject_pipeline_forward
from transformers import AutoModelForCausalLM, AutoTokenizer
from utils import generate_input_ids_batch, benchmark, format_to_gb,print_mem_usage,get_number_of_params

logger = logging.getLogger(__name__)
pippy.fx.Tracer.proxy_buffer_attributes = True

gigabyte_size = 1024**3



def run_all(pp_ranks, args):
    model = args.model
    model.eval()
    model.config.use_cache = False  # don't output `past_key_values`
    num_ranks = len(pp_ranks)

    if args.rank == 0:
        print(model.config)
        print(
            f"model total number of params = {get_number_of_params(model) // 10 ** 6}M"
        )

    split_policy = pippy.split_into_equal_size(num_ranks)

    # Use default value for kwargs other than `input_ids`
    concrete_args = pippy.create_default_args(
        model,
        except_keys="input_ids",
    )
    if "bloom" in args.model_name:
        # Used to avoid a control flow and tracing `len` call in BloomForCausalLM that looks like this:
        # `if len(deprecated_arguments) > 0:`
        concrete_args.setdefault("deprecated_arguments", {})

    pipe_driver, stage_mod = pippy.all_compile(
        model,
        num_ranks,
        args.chunks,
        split_policy=split_policy,
        tracer=PiPPyHFTracer(),
        concrete_args=concrete_args,
    )

    params = get_number_of_params(stage_mod)
    print(f"submod_{args.rank} {params // 10 ** 6}M params")

    if args.rank != 0:
        return

    # Master continues
    print_mem_usage()

    # Inject pipeline driver's forward function back to original model to support HF's `generate()` method
    inject_pipeline_forward(model, pipe_driver)

    #Generate inputs
    input_ids = generate_input_ids_batch(args.batch_size, args.seq_len)
    input_ids = input_ids.to(args.device)
    #Run benchmarks
    total_time = benchmark(model,input_ids,args)
    time_per_token = total_time/args.max_tokens
    logger.info("benchmark is done and avegrage time per tokens is %s", time_per_token)

    if os.path.exists(args.log_filename):
        output_file = open(args.log_filename, "a")
    else:
        output_file = open(args.log_filename, "w")

        output_file.write(
            "model_name, batch_size,chunks, seq_len, max_tokens,time_per_token, total_time\n"
        )

    output_file.write(
                    "{},{},{},{},{},{},{},\n".format(
                        args.model_name,
                        args.batch_size,
                        args.chunks,
                        args.seq_len,
                        args.max_tokens,
                        time_per_token,
                        total_time,
                    )
                )
    

if __name__ == "__main__":
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
    parser.add_argument("--model_name", type=str, default="facebook/opt-350m")
    parser.add_argument("--batch_size", type=int, default=1)
    parser.add_argument("--chunks", type=int, default=1)
    parser.add_argument(
        "--cuda", type=int, default=int(torch.cuda.is_available())
    )
    parser.add_argument(
        "--pp_group_size", type=int, default=int(os.getenv("WORLD_SIZE", 4))
    )
    parser.add_argument(
        "--seq_len",
        type=int,
        default=50,
        help="",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=8,
        help="",
    )

    parser.add_argument(
        "--max_tokens",
        type=int,
        default=10,
        help="",
    )

    parser.add_argument(
        "--iterations",
        type=int,
        default=50,
        help="",
    )

    parser.add_argument(
        "--log_filename",
        type=str,
        default='pippy_benchmark_logs.csv',
        help="",
    )

    args = parser.parse_args()

    assert args.world_size % args.pp_group_size == 0

    supported_model_categories = [
        "opt",
        "gpt2",
        "bloom",
        "EleutherAI/gpt",
        "codegen",
    ]
    # For example:
    # "facebook/opt-350m"
    # "gpt2"
    # "bigscience/bloom-3b"
    # EleutherAI/gpt-neo-2.7B
    # Salesforce/codegen-2B-multi

    # Main process loads model
    if any([m in args.model_name for m in supported_model_categories]):
        print(f"Loading model {args.model_name}")
        model = AutoModelForCausalLM.from_pretrained(
            args.model_name, use_cache=False
        )
    else:
        raise ValueError(f"Unsupported model: {args.model_name}")

    args.model = model
    args.gspmd = 1
    run_pippy(run_all, args)