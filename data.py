import random

from os import path, remove as delete_file
from functools import partial

from datasets import load_dataset

from tiktoken import Encoding

import numpy as np

import torch

from torch import Tensor
from torch.utils.data import IterableDataset, Dataset
from torch.nn.utils.rnn import pad_sequence

from tqdm import tqdm


class Fineweb(IterableDataset):
    """
    The Fineweb dataset by HuggingFace, tokenized and stored as a memory map for fast
    access during pre-training.
    """

    DATASET_NAME = "HuggingFaceFW/fineweb"

    SUBSETS = {"sample-10BT", "sample-100BT", "sample-350BT"}

    PADDING_INDEX = -100

    def __init__(
        self,
        tokenizer: Encoding,
        root_path: str = "./dataset",
        subset: str | None = "sample-10BT",
        split: str = "train",
        tokens_per_sample: int = 1024,
        samples_per_epoch: int = 4096,
    ):
        super().__init__()

        if subset != None:
            if subset not in self.SUBSETS:
                raise ValueError(f"Invalid subset, {subset} given.")

        if split not in {"train", "test"}:
            raise ValueError(f"Split must be either train or test, {split} given.")

        if tokens_per_sample < 1:
            raise ValueError(f"Tokens per sample must be greater than 0.")

        if samples_per_epoch < 1:
            raise ValueError(f"Samples per epoch must be greater than 0.")

        dataset_name = f"fineweb-{subset}" if subset != None else "fineweb"

        bin_path = path.join(root_path, f"{dataset_name}-{tokenizer.name}.bin")

        self.tokenizer = tokenizer

        if not path.exists(bin_path):
            tokenized_dataset = load_dataset(
                self.DATASET_NAME,
                name=subset,
                split="train",
                streaming=True,
            ).map(
                self.tokenize,
            )

            temp_path = bin_path + ".temp"

            temp_out = np.memmap(temp_path, dtype=np.uint16, mode="w+", shape=1024)

            new_memmap = partial(np.memmap, temp_path, dtype=np.uint16, mode="r+")

            total_length = 0

            for row in tqdm(tokenized_dataset, desc=f"Preprocessing dataset"):
                tokens = row["tokens"]

                length = len(tokens)

                l_hat = total_length + length

                while l_hat > len(temp_out):
                    new_temp_out = new_memmap(shape=2 * len(temp_out))

                    new_temp_out[: len(temp_out)] = temp_out

                    temp_out = new_temp_out

                temp_out[total_length:l_hat] = tokens

                total_length = l_hat

            bin_out = np.memmap(
                bin_path, dtype=np.uint16, mode="w+", shape=total_length
            )

            bin_out[:] = temp_out[:total_length]

            bin_out.flush()

            delete_file(temp_path)

        memmap = np.memmap(bin_path, dtype=np.uint16, mode="r")

        tokens_per_epoch = samples_per_epoch * (tokens_per_sample + 1)

        start = tokens_per_epoch if split == "train" else 0
        end = len(memmap) if split == "train" else tokens_per_epoch

        max_offset = end - tokens_per_epoch

        self.memmap = memmap
        self.tokens_per_sample = tokens_per_sample
        self.samples_per_epoch = samples_per_epoch
        self.start = start
        self.max_offset = max_offset

    def tokenize(self, sample: dict) -> dict:
        tokens = self.tokenizer.encode_ordinary(sample["text"])

        tokens.append(self.tokenizer.eot_token)

        return {
            "tokens": tokens,
        }

    def __iter__(self):
        start = random.randint(self.start, self.max_offset)

        for _ in range(self.samples_per_epoch):
            end = start + self.tokens_per_sample

            x = self.memmap[start:end]
            y = self.memmap[start + 1 : end + 1]

            x = x.astype(np.int64)
            y = y.astype(np.int64)

            assert x.shape == y.shape, "Sample / label shape mismatch."

            yield x, y

            start += self.tokens_per_sample


class SmolTalk(Dataset):
    """
    The SmolTalk dataset by HuggingFace formatted as ChatML messages with masked
    system and user prompts.
    """

    DATASET_NAME = "HuggingFaceTB/smoltalk"

    SUBSETS = {
        "all",
        "apigen-80k",
        "everyday-conversations",
        "explore-instruct-rewriting",
        "longalign",
        "metamathqa-50k",
        "numina-cot-100k",
        "openhermes-100k",
        "self-oss-instruct",
        "smol-constraints",
        "smol-magpie-ultra",
        "smol-rewrite",
        "smol-summarize",
        "systemchats-30k",
    }

    PADDING_INDEX = -100

    PROMPT_TEMPLATE = "<|im_start|>{role}\n{message}\n<|im_end|>\n"

    def __init__(
        self,
        tokenizer: Encoding,
        subset: str = "all",
        max_tokens_per_sample: int = 1024,
        train_on_inputs: bool = False,
    ):
        super().__init__()

        if subset not in self.SUBSETS:
            raise ValueError(f"Invalid subset, {subset} given.")

        if max_tokens_per_sample < 1:
            raise ValueError(
                f"Max tokens per sample must be greater than 0, {max_tokens_per_sample} given."
            )

        self.tokenizer = tokenizer

        self.dataset = load_dataset(self.DATASET_NAME, subset, split="train")

        self.max_tokens_per_sample = max_tokens_per_sample
        self.train_on_inputs = train_on_inputs

    def collate(self, batch: list) -> tuple[Tensor, Tensor]:
        """Custom collate function adds left padding to batched samples."""

        sample, labels = [], []

        for x, y in batch:
            sample.append(x)
            labels.append(y)

        x = pad_sequence(
            sample,
            batch_first=True,
            padding_value=self.PADDING_INDEX,
            padding_side="left",
        )
        y = pad_sequence(
            labels,
            batch_first=True,
            padding_value=self.PADDING_INDEX,
            padding_side="left",
        )

        assert x.shape == y.shape, "Sample / label batch shape mismatch."

        return x, y

    def __getitem__(self, index: int):
        row = self.dataset[index]

        samples, labels = [], []

        for message in row["messages"]:
            text = self.PROMPT_TEMPLATE.format(
                role=message["role"],
                message=message["content"],
            )

            tokens = self.tokenizer.encode(text, allowed_special="all")

            tokens.append(self.tokenizer.eot_token)

            if len(tokens) > self.max_tokens_per_sample:
                break

            samples.extend(tokens[:-1])

            if self.train_on_inputs:
                labels.extend(tokens[1:])
            else:
                labels.extend([self.PADDING_INDEX] * (len(tokens) - 1))

        x = torch.tensor(samples, dtype=torch.int64)
        y = torch.tensor(labels, dtype=torch.int64)

        assert x.shape == y.shape, "Sample / label shape mismatch."

        return x, y

    def __len__(self):
        return len(self.dataset)
