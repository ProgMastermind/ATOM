# SPDX-License-Identifier: MIT
# Copyright (C) 2026, Advanced Micro Devices, Inc. All rights reserved.

"""Torch-native attention backend for ATOM.

Purpose
-------
Provide an attention backend that does not depend on AITER's prebuilt HIP
.so files. The shipped AITER package in rocm/atom-dev:latest has prebuilt
modules for gfx94x/95x only; on gfx1201 (RDNA4) the first paged-attention
HIP load fails with 'No compatible code objects found for: gfx1201',
SIGSEGV-ing the ModelRunner subprocess before any forward pass runs.

Wiring
------
Selected by atom/utils/selector.py:get_attn_backend_cls when running on a
device whose gcnArchName is 'gfx1201', or when ATOM_TORCH_NATIVE_ATTN=1.

Status (scaffold — do not ship as a real backend yet)
-----------------------------------------------------
Today this file lays out the class structure, subclasses CommonAttentionBuilder
to inherit the prefill metadata path (which is already pure torch + Triton),
and stubs prepare_decode / build_for_cudagraph_capture / TorchNativeAttentionImpl
with NotImplementedError messages that point to the next concrete sub-task.

Remaining work, broken into commit-sized pieces (each its own session):

  TODO-1  prepare_decode: build slot_mapping + context_lens + block_tables
          for the decode batch (no aiter kv_indptr/kv_indices; we will gather
          K/V per token in the impl). Mirror aiter_attention.py:529-620,
          stripping all kv_indptr/kv_indices/persistent-worker buffers.

  TODO-2  build_for_cudagraph_capture: return AttentionMetaData with
          slot_mapping/context_lens/block_tables/cu_seqlens_q sliced to bs.
          Mirror aiter_attention.py:793-822 stripped of aiter-specific fields.

  TODO-3  TorchNativeAttentionImpl.__init__: accept the same kwargs as
          PagedAttentionImpl (atom/model_ops/attention_mha.py:29-90), store
          rotary_emb/q_norm/k_norm/scale/heads/sliding_window, allocate
          kv-cache views the runner will fill in via reshape_and_cache.

  TODO-4  TorchNativeAttentionImpl.forward: prefill path — apply RoPE,
          write K/V into the paged cache via a torch scatter on slot_mapping,
          run F.scaled_dot_product_attention with a block-diagonal causal
          mask built from cu_seqlens_q (or call the variable-length SDPA
          variant in pytorch 2.10).

  TODO-5  TorchNativeAttentionImpl.forward: decode path — apply RoPE to the
          new query, write current K/V into cache via slot_mapping, gather
          historical K/V from block_tables for each request, then run SDPA
          with a left-padding mask (no causal needed for decode).

  TODO-6  Sliding-window support — mask out positions older than
          self.sliding_window in both prefill and decode paths.

  TODO-7  KV-cache reshape_and_cache helper — replace aiter.reshape_and_cache
          with a torch index_put_ on the [num_blocks, block_size, num_heads,
          head_dim] tensor using slot_mapping. Lives wherever the existing
          aiter call site is (likely attention_mha.py:forward path).

  TODO-8  FP8 KV cache — when kv_cache_dtype='fp8', dequant K/V from FP8
          before SDPA (or quantize on write). For first usable version,
          recommend kv_cache_dtype='bf16' and defer FP8 KV.

Once TODO-1..5 land, a forward pass should complete on Llama-3.1 / Mistral-3
on gfx1201 without invoking any precompiled aiter HIP kernel for attention.
"""

from __future__ import annotations

import logging
import os
from typing import Optional, Type

import torch
from torch import nn

from atom.model_engine.scheduler import ScheduledBatch
from atom.model_ops.attentions.backends import (
    AttentionBackend,
    AttentionImpl,
    CommonAttentionBuilder,
)
from atom.utils.forward_context import AttentionMetaData

logger = logging.getLogger("atom")


def _is_gfx1201() -> bool:
    """Return True if running on a gfx1201 (RDNA4) device."""
    if not torch.cuda.is_available():
        return False
    name = torch.cuda.get_device_properties(0).gcnArchName or ""
    return name.startswith("gfx1201")


def use_torch_native_attn() -> bool:
    """Decide whether ATOM should route attention through this backend."""
    if os.environ.get("ATOM_TORCH_NATIVE_ATTN", "").lower() in ("1", "true"):
        return True
    return _is_gfx1201()


class TorchNativeBackend(AttentionBackend):
    """AITER-free attention backend. See module docstring for status."""

    @staticmethod
    def get_name() -> str:
        return "TORCH_NATIVE_ATTENTION"

    @staticmethod
    def get_builder_cls() -> Type["TorchNativeMetadataBuilder"]:
        return TorchNativeMetadataBuilder

    @staticmethod
    def get_impl_cls() -> Type["TorchNativeAttentionImpl"]:
        return TorchNativeAttentionImpl


class TorchNativeMetadataBuilder(CommonAttentionBuilder):
    """Subclass CommonAttentionBuilder so we inherit prepare_prefill (which
    already uses only torch + a Triton helper for block-table conversion).
    The aiter-specific allocations done by AiterAttentionMetadataBuilder.__init__
    (get_pa_metadata_info_v1, work_meta_data, work_indptr, kv_indptr, ...) are
    deliberately omitted — they target a paged-attention kernel that does not
    have a gfx1201 build.
    """

    def __init__(
        self,
        kv_cache_spec=None,
        layer_names=None,
        config=None,
        device=None,
        model_runner=None,
    ):
        # block_size matches the runner's block_size; we have no second-level
        # 'aiter persistent' block size to negotiate.
        self.block_size = 16 if model_runner.block_size != 1024 else 1024
        CommonAttentionBuilder.__init__(self, model_runner)
        logger.info(
            "TorchNativeMetadataBuilder: initialized (no aiter HIP allocations)"
        )

    def prepare_decode(self, batch: ScheduledBatch, bs: int):
        # TODO-1: build slot_mapping/context_lens/block_tables for decode without
        # aiter's kv_indptr/kv_indices. Mirror aiter_attention.py:prepare_decode
        # (lines ~529-620) stripped of:
        #   - kv_indptr / kv_indices fields
        #   - persistent-attention worker buffers (work_meta_data, ...)
        #   - block-size 1024 special path
        # Return (AttentionMetaData, positions_tensor).
        raise NotImplementedError(
            "TorchNativeMetadataBuilder.prepare_decode is a TODO — see "
            "module docstring 'TODO-1'."
        )

    def build_for_cudagraph_capture(self, bs: int):
        # TODO-2: return (AttentionMetaData, Context) sliced to bs from
        # self.model_runner.forward_vars.
        raise NotImplementedError(
            "TorchNativeMetadataBuilder.build_for_cudagraph_capture is a "
            "TODO — see module docstring 'TODO-2'. Workaround: run with "
            "--enforce-eager and --level 0 to skip CUDAGraph capture."
        )


class TorchNativeAttentionImpl(AttentionImpl):
    """Torch-native paged-attention forward.

    Same constructor signature as PagedAttentionImpl
    (atom/model_ops/attention_mha.py:29). Forward pass is a TODO.
    """

    def __init__(
        self,
        num_heads: int,
        head_dim: int,
        scale: float,
        num_kv_heads: Optional[int] = None,
        alibi_slopes=None,
        sliding_window: Optional[int] = None,
        kv_cache_dtype: str = "bf16",
        logits_soft_cap=None,
        attn_type=None,
        kv_sharing_target_layer_name=None,
        layer_num: int = 0,
        mla_modules=None,
        sinks=None,
        rotary_emb=None,
        q_norm=None,
        k_norm=None,
        **kwargs,
    ):
        nn.Module.__init__(self)
        # TODO-3: store all these and allocate K/V cache tensor views the
        # ModelRunner will populate via the build_kv_cache_tensor flow.
        self.num_heads = num_heads
        self.head_dim = head_dim
        self.scale = scale
        self.num_kv_heads = num_kv_heads if num_kv_heads is not None else num_heads
        self.sliding_window = sliding_window if sliding_window is not None else -1
        self.kv_cache_dtype = kv_cache_dtype
        self.layer_num = layer_num
        self.rotary_emb = rotary_emb
        self.q_norm = q_norm
        self.k_norm = k_norm
        # KV cache slabs are populated by ModelRunner after backend.build_kv_cache_tensor.
        self.k_cache = torch.tensor([])
        self.v_cache = torch.tensor([])
        if kv_cache_dtype == "fp8":
            logger.warning(
                "TorchNativeAttentionImpl: kv_cache_dtype=fp8 is a TODO; "
                "use --kv_cache_dtype bf16 for now (TODO-8)."
            )

    def forward(
        self,
        query: torch.Tensor,
        key: torch.Tensor,
        value: torch.Tensor,
        position: Optional[torch.Tensor] = None,
    ) -> torch.Tensor:
        # TODO-4 (prefill) + TODO-5 (decode) + TODO-6 (sliding window):
        # 1. Apply RoPE to query/key (or trust caller already did it; check
        #    PagedAttentionImpl.forward for which side does RoPE).
        # 2. Write current K/V into self.k_cache / self.v_cache via slot_mapping.
        # 3. Gather historical K/V from block_tables.
        # 4. F.scaled_dot_product_attention with the right mask
        #    (block-diagonal causal for prefill; left-padding mask for decode).
        # 5. Apply sliding-window mask if self.sliding_window > 0.
        raise NotImplementedError(
            "TorchNativeAttentionImpl.forward is a TODO — see module "
            "docstring 'TODO-4 / TODO-5'. Currently the backend builds "
            "successfully but the first attention call will trip this."
        )
