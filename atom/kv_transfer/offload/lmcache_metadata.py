# SPDX-License-Identifier: MIT
# Copyright (C) 2024-2025, Advanced Micro Devices, Inc. All rights reserved.

"""LMCache metadata wrapper for ATOM raw-byte KV offload."""

from __future__ import annotations

from typing import Any

import torch


def _cdiv(a: int, b: int) -> int:
    return -(-int(a) // int(b))


class ATOMRawBytesLMCacheMetadata:
    """Proxy around ``LMCacheMetadata`` with ATOM raw-byte allocation shapes."""

    def __init__(
        self,
        base_metadata: Any,
        *,
        atom_block_size: int,
        bytes_per_block: int,
    ) -> None:
        self._atom_base_metadata = base_metadata
        self.__dict__.update(vars(base_metadata))
        self.atom_block_size = int(atom_block_size)
        self.atom_bytes_per_block = int(bytes_per_block)
        chunk_size = int(getattr(base_metadata, "chunk_size"))
        if self.atom_block_size <= 0:
            raise ValueError("ATOM raw-byte metadata: atom_block_size must be > 0")
        if self.atom_bytes_per_block <= 0:
            raise ValueError("ATOM raw-byte metadata: bytes_per_block must be > 0")
        if chunk_size % self.atom_block_size != 0:
            raise ValueError(
                "LMCache chunk size must be divisible by ATOM KV block size: "
                f"chunk_size={chunk_size}, block_size={self.atom_block_size}"
            )

    def __getattr__(self, name: str) -> Any:
        return getattr(self._atom_base_metadata, name)

    def __eq__(self, other: object) -> bool:
        if isinstance(other, ATOMRawBytesLMCacheMetadata):
            return (
                self._atom_base_metadata == other._atom_base_metadata
                and self.atom_block_size == other.atom_block_size
                and self.atom_bytes_per_block == other.atom_bytes_per_block
            )
        return False

    def is_first_rank(self) -> bool:
        return self._atom_base_metadata.is_first_rank()

    def get_dtypes(self) -> list[torch.dtype]:
        return [torch.uint8]

    def get_shapes(self, num_tokens: int | None = None) -> list[torch.Size]:
        if num_tokens is None:
            num_tokens = int(self.chunk_size)
        nblocks = _cdiv(int(num_tokens), self.atom_block_size)
        return [torch.Size((nblocks * self.atom_bytes_per_block,))]

    def get_num_groups(self) -> int:
        return 1
