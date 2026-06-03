# SPDX-License-Identifier: MIT
# Copyright (C) 2024-2025, Advanced Micro Devices, Inc. All rights reserved.

"""Optional HIP fused chunk-major staging for ATOM LMCache offload."""

from __future__ import annotations

from pathlib import Path

from torch.utils.cpp_extension import load

_EXT = None


def _load_ext():
    global _EXT
    if _EXT is None:
        base = Path(__file__).parent
        _EXT = load(
            name="atom_lmcache_native_kv_staging",
            sources=[
                str(base / "native_kv_staging.cpp"),
                str(base / "native_kv_staging_kernel.hip"),
            ],
            extra_cflags=["-O3"],
            extra_cuda_cflags=["-O3"],
            verbose=False,
        )
    return _EXT


def load_extension() -> None:
    _load_ext()


def fused_pack_chunk_major(
    segment_tensors,
    segment_block_bytes,
    chunk_block_counts,
    block_ids,
    device_buf,
) -> None:
    _load_ext().fused_pack_chunk_major(
        segment_tensors,
        [int(x) for x in segment_block_bytes],
        [int(x) for x in chunk_block_counts],
        [int(x) for x in block_ids],
        device_buf,
    )


def fused_unpack_chunk_major(
    device_buf,
    segment_tensors,
    segment_block_bytes,
    chunk_block_counts,
    block_ids,
) -> None:
    _load_ext().fused_unpack_chunk_major(
        device_buf,
        segment_tensors,
        [int(x) for x in segment_block_bytes],
        [int(x) for x in chunk_block_counts],
        [int(x) for x in block_ids],
    )
