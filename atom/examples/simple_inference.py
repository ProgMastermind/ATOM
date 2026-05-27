# SPDX-License-Identifier: MIT
# Copyright (C) 2024-2025, Advanced Micro Devices, Inc. All rights reserved.

import argparse

from atom import SamplingParams
from atom.model_engine.arg_utils import EngineArgs
from transformers import AutoTokenizer

parser = argparse.ArgumentParser(
    formatter_class=argparse.RawTextHelpFormatter,
    description="config of test",
)

# Add engine arguments
EngineArgs.add_cli_args(parser)

# Add example-specific arguments
parser.add_argument(
    "--temperature", type=float, default=0.6, help="temperature for sampling"
)


def generate_cuda_graph_sizes(max_size):
    # This is for DP split batch size
    sizes = []
    power = 1
    while power <= max_size:
        sizes.append(power)
        power *= 2
    return sizes


def main():
    args = parser.parse_args()

    tokenizer = AutoTokenizer.from_pretrained(args.model)

    dummy_token = "hi "
    dummy_tokens = tokenizer.encode(dummy_token, add_special_tokens=False)
    target_len = 1024
    repeat_count = target_len // len(dummy_tokens)
    dummy_text = dummy_token * repeat_count
    token_count = len(tokenizer.encode(dummy_text, add_special_tokens=False))
    print(f"Input token count: {token_count}")

    prompts_raw = [dummy_text]

    args.cudagraph_capture_sizes = str(generate_cuda_graph_sizes(len(prompts_raw)))

    engine_args = EngineArgs.from_cli_args(args)
    llm = engine_args.create_engine()

    sampling_params = SamplingParams(temperature=args.temperature, max_tokens=1024)

    prompts = [
        tokenizer.apply_chat_template(
            [{"role": "user", "content": prompt}],
            tokenize=False,
            add_generation_prompt=True,
            enable_thinking=True,
        )
        for prompt in prompts_raw
    ]
    print("This is prompts (truncated):", [p[:200] + "..." for p in prompts])
    # print("Warming up...")
    # _ = llm.generate(["warmup"], sampling_params)
    # print("Warm up done")

    print("\n" + "=" * 70)
    print("Starting profiling...")
    print("=" * 70)

    llm.start_profile()
    outputs = llm.generate(prompts, sampling_params)
    llm.stop_profile()

    for prompt, output in zip(prompts, outputs):
        print("\n")
        print(f"Prompt: {prompt!r}")
        print(f"Completion: {output['text']!r}")

    llm.print_mtp_statistics()


if __name__ == "__main__":
    main()
