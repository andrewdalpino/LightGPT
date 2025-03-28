import random

from os import path
from argparse import ArgumentParser

import torch

from torch.cuda import is_available as cuda_is_available

from model import LightGPT


def main():
    parser = ArgumentParser(
        description="Use a greedy search strategy to generate candidate sequences from the base model.",
    )

    parser.add_argument(
        "--checkpoint_path", default="./checkpoints/checkpoint.pt", type=str
    )
    parser.add_argument("--max_tokens", default=200, type=int)
    parser.add_argument("--context_length", default=1024, type=int)
    parser.add_argument("--num_candidates", default=3, type=int)
    parser.add_argument("--beam_width", default=16, type=int)
    parser.add_argument("--length_penalty", default=1.0, type=float)
    parser.add_argument("--device", default="cuda", type=str)
    parser.add_argument("--seed", default=None, type=int)

    args = parser.parse_args()

    if "cuda" in args.device and not cuda_is_available():
        raise RuntimeError("Cuda is not available.")

    torch.set_float32_matmul_precision("high")

    if args.seed:
        torch.manual_seed(args.seed)
        random.seed(args.seed)

    checkpoint = torch.load(
        args.checkpoint_path, map_location=args.device, weights_only=True
    )

    tokenizer = checkpoint["tokenizer"]

    eos_indices = {tokenizer.eot_token}

    model = LightGPT(**checkpoint["model_args"])

    model = torch.compile(model)

    model.load_state_dict(checkpoint["model"])

    print("Model checkpoint loaded")

    model.to(args.device)

    model.eval()

    while True:
        prompt = input("Enter a prompt: ")

        prompt = tokenizer.encode_ordinary(prompt)

        prompt = torch.tensor(prompt, dtype=torch.int64, device=args.device)

        candidates = model.beam_search(
            prompt,
            args.max_tokens,
            args.context_length,
            args.num_candidates,
            args.beam_width,
            args.length_penalty,
            eos_indices,
        )

        for i, candidate in enumerate(candidates, start=1):
            print(f"Sequence #{i}")

            out = tokenizer.decode(candidate.tokens.tolist()).strip()

            print(out, end="\n\n")

        print("\n")

        if "y" not in input("Go again? (yes|no): ").lower():
            break


if __name__ == "__main__":
    main()
