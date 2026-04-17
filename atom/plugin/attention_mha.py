# SPDX-License-Identifier: MIT
# Copyright (C) 2024-2025, Advanced Micro Devices, Inc. All rights reserved.

"""
Plugin mode extensions for PagedAttentionImpl.
This module provides additional methods for PagedAttentionImpl when running in plugin mode.
"""

import torch
import aiter
from aiter import dtypes, fused_qk_norm_rope_cache_quant_shuffle
from aiter.ops.triton.fused_kv_cache import fused_qk_rope_reshape_and_cache
from aiter.ops.triton.gluon.pa_decode_gluon import get_recommended_splits
from typing import TYPE_CHECKING, Optional
from atom.utils import envs
from atom.model_ops.base_attention import cp_mha_gather_cache
import triton
import triton.language as tl
import logging

logger = logging.getLogger("atom")

@triton.jit
def reshape_and_cache_shuffle_kernel(
    key_ptr,  # [num_tokens, num_kv_heads, head_size]
    value_ptr,  # [num_tokens, num_kv_heads, head_size]
    key_cache_ptr,  # [num_blocks, num_kv_heads, head_size // x, block_size, x]
    value_cache_ptr,  # [num_blocks, num_kv_heads, block_size // x, head_size, x]
    slot_mapping_ptr,  # [num_tokens]
    k_scale_ptr,  # [num_blocks, num_kv_heads, block_size]
    v_scale_ptr,  # [num_blocks, num_kv_heads, block_size]
    x,
    k_stride0,
    v_stride0,
    block_size,
    head_size,
    num_kv_heads,
    kcache_block_stride,  # key_cache.stride(0) — actual elements between blocks
    vcache_block_stride,  # value_cache.stride(0) — actual elements between blocks
    BLOCK_SIZE: tl.constexpr,
    QUANT: tl.constexpr,
    IS_FNUZ: tl.constexpr,
):
    tid = tl.program_id(0)
    head_id = tl.program_id(1)
    offset = tl.arange(0, BLOCK_SIZE)
    src_offset_k = tid * k_stride0 + head_id * head_size
    src_offset_v = tid * v_stride0 + head_id * head_size
    slot_id = tl.load(slot_mapping_ptr + tid)
    if slot_id < 0:
        return
    block_id = slot_id // block_size
    block_offset = slot_id % block_size
    # Use actual block stride instead of computed product of inner dimensions.
    # When KV cache shares memory with mamba/linear attention layers (hybrid
    # models like Qwen3.5), blocks are not contiguous — stride[0] > product of
    # inner dims.  Using the real stride ensures we write to the correct offset.
    dst_k_offset = (
        block_id * kcache_block_stride
        + head_id * head_size * block_size
    )
    dst_v_offset = (
        block_id * vcache_block_stride
        + head_id * head_size * block_size
    )
    dst_k_shuffle_offset = (
        dst_k_offset + offset // x * block_size * x + block_offset * x + offset % x
    )
    # v_cache layout is [num_blocks, num_kv_heads, head_size, block_size]
    # (plain, NOT shuffled — only k_cache uses shuffle layout)
    dst_v_shuffle_offset = (
        dst_v_offset
        + offset * block_size
        + block_offset
    )
    k_val = tl.load(key_ptr + src_offset_k + offset)
    v_val = tl.load(value_ptr + src_offset_v + offset)
    if QUANT:
        k_scale = 1.0
        v_scale = 1.0
        k_dtype = key_cache_ptr.type.element_ty
        v_dtype = value_cache_ptr.type.element_ty
        k_val = (k_val.to(tl.float32) / k_scale).to(k_dtype)
        v_val = (v_val.to(tl.float32) / v_scale).to(v_dtype)
    tl.store(key_cache_ptr + dst_k_shuffle_offset, k_val)
    tl.store(value_cache_ptr + dst_v_shuffle_offset, v_val)

def reshape_and_cache_shuffle_triton(
    key: torch.Tensor,
    value: torch.Tensor,
    key_cache: torch.Tensor,
    value_cache: torch.Tensor,
    slot_mapping: torch.Tensor,
    kv_cache_dtype: str,
    k_scales: torch.Tensor,
    v_scales: torch.Tensor,
):
    num_tokens = slot_mapping.shape[0]
    _, num_kv_heads, head_size = key.shape
    num_blocks, _, _, block_size, x = key_cache.shape
    QUANT = False
    if kv_cache_dtype.startswith("fp8"):
        QUANT = True
    grid = (
        num_tokens,
        num_kv_heads,
    )
    reshape_and_cache_shuffle_kernel[grid](
        key,
        value,
        key_cache,
        value_cache,
        slot_mapping,
        k_scales,
        v_scales,
        x,
        key.stride(0),
        value.stride(0),
        block_size,
        head_size,
        num_kv_heads,
        key_cache.stride(0),
        value_cache.stride(0),
        BLOCK_SIZE=head_size,
        QUANT=QUANT,
        IS_FNUZ=True,
    )

if TYPE_CHECKING:
    from atom.utils.forward_context import AttentionMetaData

ATOM_ENABLE_QK_NORM_ROPE_CACHE_QUANT_FUSION = (
    envs.ATOM_ENABLE_QK_NORM_ROPE_CACHE_QUANT_FUSION
)

_PARTITION_SIZE_ROCM = 256
_CP_TOKENS_PER_ITER_ROCM = 32 * 1024
_QWEN_GLUON_PA_DECODE_BS = 64


def _is_fp8_kv_cache(kv_cache_dtype: str) -> bool:
    return kv_cache_dtype.startswith("fp8")


class PagedAttentionImplPluginModeMethods:
    """
    Container class for plugin mode methods.
    This class cannot be instantiated - it only serves as a namespace for methods
    that will be added to PagedAttentionImpl via decorator.
    """

    def __init__(self):
        raise TypeError(
            "PagedAttentionImplPluginModeMethods cannot be instantiated. "
            "It is only used as a method container for the decorator."
        )

    # this method will just be called by vLLM and there is no logic in this method
    # as ATOM handles the process after loading weights for all ops by itself
    def process_weights_after_loading(self, act_dtype: torch.dtype = torch.bfloat16):
        pass

    def rope_cache_plugin_mode(
        self,
        q: torch.Tensor,
        k: torch.Tensor,
        v: torch.Tensor,
        qkv: torch.Tensor,
        position: torch.Tensor,
        attention_metadata: "AttentionMetaData",
        k_cache: torch.Tensor,
        v_cache: torch.Tensor,
        k_scale: torch.Tensor,
        v_scale: torch.Tensor,
        flash_layout: bool = False,
    ):
        num_blocks, block_size, num_kv_heads, head_size = k_cache.shape

        if not flash_layout:
            x = 16 // k_cache.element_size()
            k_cache_template = torch.empty(
                [num_blocks, num_kv_heads, head_size // x, block_size, x],
                dtype=k_cache.dtype,
                device="meta",
            )
            new_key_cache = k_cache.view_as(k_cache_template)
            # ATOM v_cache layout is 4D: [num_blocks, num_kv_heads, head_size, block_size]
            # This matches what reshape_and_cache_with_pertoken_quant and
            # pa_decode_gluon expect with asm_layout=True.
            v_cache_template = torch.empty(
                [num_blocks, num_kv_heads, head_size, block_size],
                dtype=v_cache.dtype,
                device="meta",
            )
            new_value_cache = v_cache.view_as(v_cache_template)
        else:
            new_key_cache = k_cache
            new_value_cache = v_cache

        # After view_as, the cache shapes are:
        # key_cache:   [num_blocks, num_kv_heads, head_size // x, block_size, x]
        # value_cache: [num_blocks, num_kv_heads, head_size, block_size]
        # This matches ATOM server mode's layout expectations.

        attn_metadata = attention_metadata

        use_triton_attn = self.sliding_window != -1 or self.head_dim != 128
        self.use_triton_attn = use_triton_attn

        if (
            self.rotary_emb is not None
            and self.q_norm is not None
            and self.k_norm is not None
        ):
            fused_qk_norm_rope_cache_quant_shuffle(
                qkv,
                num_heads_q=self.num_heads,
                num_heads_k=self.num_kv_heads,
                num_heads_v=self.num_kv_heads,
                head_dim=self.head_dim,
                eps=self.q_norm.eps,
                qw=self.q_norm.weight,
                kw=self.k_norm.weight,
                cos_sin_cache=self.rotary_emb.cos_sin_cache,
                is_neox_style=self.rotary_emb.is_neox_style,
                pos_ids=position,
                k_cache=new_key_cache,
                v_cache=new_value_cache,
                slot_mapping=attn_metadata.slot_mapping,
                kv_cache_dtype=(
                    "auto" if self.kv_cache_dtype == "bf16" else self.kv_cache_dtype
                ),
                k_scale=k_scale,
                v_scale=v_scale,
            )

            qkv = qkv.view(qkv.shape[0], -1, self.head_dim)
            q, k, v = qkv.split(
                [self.num_heads, self.num_kv_heads, self.num_kv_heads], dim=1
            )
        elif use_triton_attn and self.rotary_emb is not None:

            k_scale = v_scale = self.per_tensor_scale
            self.per_token_quant = False
            qkv = qkv.view(qkv.shape[0], -1, self.head_dim)
            q, k, v = qkv.split(
                [self.num_heads, self.num_kv_heads, self.num_kv_heads], dim=1
            )
            q, k, _k_cache, _v_cache = fused_qk_rope_reshape_and_cache(
                q,
                k,
                v,
                new_key_cache,
                new_value_cache,
                attn_metadata.slot_mapping,
                position,
                self.rotary_emb.cos_cache,
                self.rotary_emb.sin_cache,
                k_scale,
                v_scale,
                self.rotary_emb.is_neox_style,
                flash_layout=flash_layout,
                apply_scale=self.kv_cache_dtype.startswith("fp8"),
                offs=None,
                q_out=q,
                k_out=k,
                output_zeros=False,
            )
        else:
            if self.q_norm is not None:
                q = self.q_norm(q)
            if self.k_norm is not None:
                k = self.k_norm(k)
            # Match server mode: use asm_layout only for asm paged attention,
            # not for triton/gluon decode (which expects non-shuffled v_cache).
            asm_layout = not use_triton_attn
            if _is_fp8_kv_cache(self.kv_cache_dtype):
                if not hasattr(self, '_debug_logged'):
                    logger.warning(
                        f"FP8 cache write: k.shape={k.shape}, v.shape={v.shape}, "
                        f"k_cache.shape={new_key_cache.shape}, v_cache.shape={new_value_cache.shape}, "
                        f"k_scale.shape={k_scale.shape if k_scale is not None else None}, "
                        f"v_scale.shape={v_scale.shape if v_scale is not None else None}, "
                        f"asm_layout={asm_layout}, use_triton_attn={use_triton_attn}, "
                        f"head_dim={self.head_dim}, kv_cache_dtype={self.kv_cache_dtype}"
                    )
                    self._debug_logged = True
                aiter.reshape_and_cache_with_pertoken_quant(
                    k,
                    v,
                    new_key_cache,
                    new_value_cache,
                    k_scale,
                    v_scale,
                    attn_metadata.slot_mapping,
                    asm_layout=asm_layout,
                )
            else:
                aiter.reshape_and_cache(
                    k,
                    v,
                    new_key_cache,
                    new_value_cache,
                    attn_metadata.slot_mapping,
                    kv_cache_dtype="auto",
                    k_scale=None,
                    v_scale=None,
                    asm_layout=asm_layout,
                )

        return q, k, v, k_cache, v_cache, k_scale, v_scale

    def paged_attention_triton_plugin_mode(
        self,
        q: torch.Tensor,
        k_cache: torch.Tensor,
        v_cache: torch.Tensor,
        k_scale: torch.Tensor,
        v_scale: torch.Tensor,
        out: torch.Tensor,
        attn_metadata: "AttentionMetaData",
    ):
        o = out
        num_seqs, num_q_heads_total, head_size = q.shape
        num_blocks, num_kv_heads, _, block_size, _ = k_cache.shape
        query_group_size = num_q_heads_total // num_kv_heads
        assert num_q_heads_total % num_kv_heads == 0

        max_context_partition_num = get_recommended_splits(num_seqs, num_kv_heads)

        context_partition_size = 256
        if self.sliding_window > 0:
            max_context_partition_num = 1
            context_partition_size = 128

        # Output buffers (same as Triton)
        intermediate_shape = (
            num_seqs,
            num_kv_heads,
            max_context_partition_num,
            query_group_size,
        )
        exp_sums = torch.empty(intermediate_shape, dtype=torch.float32, device=q.device)
        max_logits = torch.empty(
            intermediate_shape, dtype=torch.float32, device=q.device
        )
        temporary_output = torch.empty(
            *intermediate_shape,
            head_size,
            dtype=q.dtype,
            device=q.device,
        )

        per_tensor = False
        if k_scale is not None and k_scale.numel() > 1:
            k_scale = k_scale.unsqueeze(-1)
            v_scale = v_scale.unsqueeze(-1)
        is_bf16_kv = not _is_fp8_kv_cache(self.kv_cache_dtype)
        compute_type = (
            torch.bfloat16
            if is_bf16_kv or per_tensor
            else aiter.dtypes.fp8
        )

        num_decode_seqs = q.shape[0]
        seq_lens_decode = attn_metadata.plugin_metadata.seq_lens[:num_decode_seqs]
        block_tables_decode = attn_metadata.plugin_metadata.block_table[
            :num_decode_seqs
        ]

        torch.ops.aiter.pa_decode_gluon(
            o,
            q,
            k_cache,
            v_cache,
            seq_lens_decode,
            block_tables_decode,
            self.scale,
            1,  # query_lenth
            max_context_partition_num,
            context_partition_size,
            compute_type,
            None,
            None if is_bf16_kv else k_scale,
            None if is_bf16_kv else v_scale,
            exp_sums=exp_sums,
            max_logits=max_logits,
            temporary_output=temporary_output,
            alibi_slopes=None,
            sinks=self.sinks,
            sliding_window=self.sliding_window,
            ps=True,
        )
        return o

    def paged_attention_asm_plugin_mode(
        self,
        q: torch.Tensor,
        k_cache: torch.Tensor,
        v_cache: torch.Tensor,
        k_scale: torch.Tensor,
        v_scale: torch.Tensor,
        num_decodes: int,
        num_decode_tokens: int,
        attn_metadata: "AttentionMetaData",
        out: torch.Tensor,
    ):
        aiter.pa_fwd_asm(
            Q=q,
            K=k_cache,
            V=v_cache,
            block_tables=attn_metadata.plugin_metadata.block_table[:num_decodes],
            context_lens=attn_metadata.plugin_metadata.seq_lens[:num_decodes],
            block_tables_stride0=attn_metadata.plugin_metadata.block_table[
                :num_decodes
            ].stride(0),
            K_QScale=k_scale,
            V_QScale=v_scale,
            out_=out[:num_decode_tokens],
            high_precision=0,
        )

        return

    def paged_attention_flash_plugin_mode(
        self,
        q: torch.Tensor,
        k_cache: torch.Tensor,
        v_cache: torch.Tensor,
        k_scale: torch.Tensor,
        v_scale: torch.Tensor,
        num_decodes: int,
        num_decode_tokens: int,
        attn_metadata: "AttentionMetaData",
        out: torch.Tensor,
    ):
        """Decode attention for bf16 kv cache in vLLM flash layout
        [num_blocks, block_size, num_kv_heads, head_size]."""
        num_seqs = num_decodes
        _, num_heads, head_size = q.shape
        max_seq_len = attn_metadata.plugin_metadata.seq_lens[:num_decodes].max().item()
        max_num_partitions = (max_seq_len + _PARTITION_SIZE_ROCM - 1) // _PARTITION_SIZE_ROCM
        nbytes_per_qo_elem = q.element_size()
        workspace_buffer = torch.empty(
            (num_seqs * num_heads * max_num_partitions * head_size) * nbytes_per_qo_elem
            + 2 * (num_seqs * num_heads * max_num_partitions) * 4,
            dtype=torch.uint8,
            device=q.device,
        )
        # query_start_loc: cumulative start positions for each seq (num_decodes+1 entries)
        query_start_loc = attn_metadata.plugin_metadata.query_start_loc[:num_decodes]
        import aiter  # noqa: F401
        torch.ops.aiter.paged_attention_v1(
            out[:num_decode_tokens],
            workspace_buffer,
            q[:num_decode_tokens],
            k_cache,
            v_cache,
            self.scale,
            attn_metadata.plugin_metadata.block_table[:num_decodes],
            query_start_loc,
            attn_metadata.plugin_metadata.seq_lens[:num_decodes],
            max_seq_len,
            None,  # alibi_slopes
            self.kv_cache_dtype,
            "NHD",
            0.0,  # logits_soft_cap
            k_scale if k_scale is not None else torch.tensor(1.0, device=q.device),
            v_scale if v_scale is not None else torch.tensor(1.0, device=q.device),
            None,  # fp8_out_scale
            _PARTITION_SIZE_ROCM,
            1,  # max_query_len
            self.sliding_window if self.sliding_window > 0 else 0,
        )

        return

    def extend_for_sliding_window(
        self,
        attn_metadata: "AttentionMetaData",
        query: torch.Tensor,
        key_cache,
        value_cache,
        output: torch.Tensor,
        cu_seqlens_q: torch.Tensor,
        max_seqlen_q: int,
        block_table: torch.Tensor,
        k_scale: Optional[torch.Tensor],
        v_scale: Optional[torch.Tensor],
    ):
        assert attn_metadata.plugin_metadata.extend_metadata is not None
        assert (
            attn_metadata.plugin_metadata.extend_metadata.chunk_context_metadata
            is not None
        )
        chunked_metadata = (
            attn_metadata.plugin_metadata.extend_metadata.chunk_context_metadata
        )
        swa_metadata = chunked_metadata.swa_metadata
        assert swa_metadata is not None
        swa_cu_seqlens = swa_metadata.swa_cu_seqlens
        swa_seq_starts = swa_metadata.swa_seq_starts
        swa_token_to_batch = swa_metadata.swa_token_to_batch
        swa_max_seqlens = swa_metadata.swa_max_seqlens
        swa_total_tokens = swa_metadata.swa_total_tokens
        key_fetched, value_fetched = (
            swa_metadata.swa_workspace[0],
            swa_metadata.swa_workspace[1],
        )

        cp_mha_gather_cache(
            key_cache=key_cache,
            value_cache=value_cache,
            key=key_fetched,
            value=value_fetched,
            block_tables=block_table,
            k_scales=k_scale,
            v_scales=v_scale,
            cu_seqlens_kv=swa_cu_seqlens,
            token_to_batch=swa_token_to_batch,
            seq_starts=swa_seq_starts,
            dequant=self.kv_cache_dtype.startswith("fp8"),
            kv_cache_layout="SHUFFLE",
            total_tokens=swa_total_tokens,
            per_token_quant=self.per_token_quant,
        )

        sliding_window = (
            (self.sliding_window, 0, 0)
            if self.sliding_window is not None
            else (-1, -1, 0)
        )
        aiter.flash_attn_varlen_func(
            q=query,
            k=key_fetched,
            v=value_fetched,
            cu_seqlens_q=cu_seqlens_q,
            cu_seqlens_k=swa_cu_seqlens,
            max_seqlen_q=max_seqlen_q,
            max_seqlen_k=swa_max_seqlens,  # need to confirm
            min_seqlen_q=1,
            dropout_p=0.0,
            softmax_scale=self.scale,
            causal=True,
            window_size=sliding_window,
            alibi_slopes=self.alibi_slopes,
            sink_ptr=self.sinks,
            return_lse=False,
            out=output,
        )

    def extend_forward(
        self,
        attn_metadata: "AttentionMetaData",
        query: torch.Tensor,
        key: torch.Tensor,
        value: torch.Tensor,
        key_cache: torch.Tensor,
        value_cache: torch.Tensor,
        output: torch.Tensor,
        cu_seqlens_q: torch.Tensor,
        max_seqlen_q: int,
        max_seqlen_k: int,
        min_seqlen_q: int,
        block_table: torch.Tensor,
        slot_mapping: torch.Tensor,
        k_scale: Optional[torch.Tensor],
        v_scale: Optional[torch.Tensor],
    ):
        from vllm.v1.attention.ops.merge_attn_states import merge_attn_states

        if self.sliding_window != -1:
            self.extend_for_sliding_window(
                attn_metadata,
                query,
                key_cache,
                value_cache,
                output,
                cu_seqlens_q,
                max_seqlen_q,
                block_table,
                k_scale,
                v_scale,
            )
            return
        out, lse = aiter.flash_attn_varlen_func(
            q=query,
            k=key,
            v=value,
            cu_seqlens_q=cu_seqlens_q,
            cu_seqlens_k=cu_seqlens_q,
            max_seqlen_q=max_seqlen_q,
            max_seqlen_k=max_seqlen_q,  # need to confirm
            min_seqlen_q=min_seqlen_q,
            dropout_p=0.0,
            softmax_scale=self.scale,
            causal=True,
            sink_ptr=self.sinks,
            alibi_slopes=self.alibi_slopes,
            return_lse=True,
        )
        assert attn_metadata.plugin_metadata.extend_metadata is not None
        chunk_context_metadata = (
            attn_metadata.plugin_metadata.extend_metadata.chunk_context_metadata
        )
        num_chunks = chunk_context_metadata.num_chunks
        workspace = chunk_context_metadata.workspace
        cu_seqlens_kv = chunk_context_metadata.cu_seq_lens_chunk
        max_seqlens = chunk_context_metadata.max_seq_lens
        chunk_starts = chunk_context_metadata.chunk_starts
        token_to_batch = chunk_context_metadata.token_to_batch
        total_token_per_batch = chunk_context_metadata.total_token_per_batch
        key_fetched, value_fetched = workspace[0], workspace[1]
        chunked_output = None
        chunked_lse = None
        for chunk_idx in range(num_chunks):
            cp_mha_gather_cache(
                key_cache=key_cache,
                value_cache=value_cache,
                key=key_fetched,
                value=value_fetched,
                block_tables=block_table,
                k_scales=k_scale,
                v_scales=v_scale,
                cu_seqlens_kv=cu_seqlens_kv[chunk_idx],
                token_to_batch=token_to_batch[chunk_idx],
                seq_starts=chunk_starts[chunk_idx],
                dequant=self.kv_cache_dtype.startswith("fp8"),
                kv_cache_layout="SHUFFLE",
                total_tokens=total_token_per_batch[chunk_idx],
                per_token_quant=self.per_token_quant,
            )

            suf_out, suf_lse = aiter.flash_attn_varlen_func(
                q=query,
                k=key_fetched,
                v=value_fetched,
                cu_seqlens_q=cu_seqlens_q,
                cu_seqlens_k=cu_seqlens_kv[chunk_idx],
                max_seqlen_q=max_seqlen_q,
                max_seqlen_k=max_seqlens[chunk_idx],
                min_seqlen_q=min_seqlen_q,
                dropout_p=0.0,
                softmax_scale=self.scale,
                causal=False,
                window_size=(-1, -1, 0),
                sink_ptr=self.sinks,
                alibi_slopes=self.alibi_slopes,
                return_lse=True,
            )

            if chunked_output is None:
                chunked_output = suf_out
                chunked_lse = suf_lse
            else:
                tmp_output = torch.empty_like(out)
                tmp_lse = torch.empty_like(lse)
                merge_attn_states(
                    output=tmp_output,
                    output_lse=tmp_lse,
                    prefix_output=chunked_output,
                    prefix_lse=chunked_lse,
                    suffix_output=suf_out,
                    suffix_lse=suf_lse,
                )
                chunked_output = tmp_output
                chunked_lse = tmp_lse

        merge_attn_states(
            output=output,
            prefix_output=chunked_output,
            prefix_lse=chunked_lse,
            suffix_output=out,
            suffix_lse=lse,
        )

    def forward_impl_plugin_mode(
        self,
        layer: torch.nn.Module,
        query: torch.Tensor,
        key: torch.Tensor,
        value: torch.Tensor,
        kv_cache: torch.Tensor,
        attn_metadata: "AttentionMetaData" = None,
        position: torch.Tensor = None,
        q_scale: torch.Tensor = None,
        qkv: torch.Tensor = None,
        output: torch.Tensor = None,
    ):
        # create the output here, it use query shape
        num_tokens = query.shape[0]
        output_dtype = query.dtype
        output_shape = torch.Size((num_tokens, self.num_heads * self.head_size))
        output = torch.empty(output_shape, dtype=output_dtype, device=query.device)

        # dummy run will skip attention in cuda graph capture phase
        if attn_metadata is None:
            return output.fill_(0)

        # when using this optimization, the qkv tensor and
        # position tensor are passed through q,k,v
        # when not using this optimization, the position is not
        # needed as the ROPE has been calculated outside of attention
        if ATOM_ENABLE_QK_NORM_ROPE_CACHE_QUANT_FUSION:
            assert (
                position is None
            ), "position should be None because it is passed through k"

            position = key
            qkv = value

            q_size = self.num_heads * self.head_dim
            kv_size = self.num_kv_heads * self.head_dim
            query, key, value = torch.split(qkv, [q_size, kv_size, kv_size], dim=-1)

        query = query.view(-1, self.num_heads, self.head_dim)
        key = key.view(-1, self.num_kv_heads, self.head_dim)
        value = value.view(-1, self.num_kv_heads, self.head_dim)
        output = output.view(-1, self.num_heads, self.head_dim)

        num_actual_tokens = attn_metadata.plugin_metadata.num_actual_tokens
        k_cache, v_cache = kv_cache.unbind(0)
        num_blocks, block_size, num_kv_heads, _ = k_cache.shape

        if self.kv_cache_dtype == "fp8":
            target_dtype = dtypes.d_dtypes[self.kv_cache_dtype]
            k_cache = k_cache.view(target_dtype)
            v_cache = v_cache.view(target_dtype)

        # create kv scale according to the num_blocks
        # usually it is created when cuda graph capture for decode phase
        if self.kv_cache_dtype == "fp8":
            if self.k_scale is None or self.v_scale is None:
                # origin kv_scale is per tensor scale of value one.
                self.per_tensor_scale = self.kv_scale
                self.kv_scale = torch.zeros(
                    2,
                    num_blocks,
                    num_kv_heads,
                    block_size,
                    dtype=dtypes.fp32,
                    device=self.device,
                )
            # update the layer kv scale tensor
            self.k_scale = self.kv_scale[0]
            self.v_scale = self.kv_scale[1]
            layer.k_scale = self.k_scale
            layer.v_scale = self.v_scale

        # as vLLM cuda graph capture padding mechanism, here split the qkvo with
        # the actual tokens
        query = query[:num_actual_tokens]
        # vLLM can call plugin attention without fused qkv/position tensors for
        # some dense-model paths (for example Llama). Slice them only when present.
        if qkv is not None:
            qkv = qkv[:num_actual_tokens]
        if position is not None:
            position = position[:num_actual_tokens]
        if key is not None:
            key = key[:num_actual_tokens]
        if value is not None:
            value = value[:num_actual_tokens]
        output_actual_tokens = output[:num_actual_tokens]

        # rope and cache flush fusion. ATOM always use shuffle layout for kv cache
        result = self.rope_cache_plugin_mode(
            q=query,
            k=key,
            v=value,
            qkv=qkv,
            position=position,
            attention_metadata=attn_metadata,
            k_cache=k_cache,
            v_cache=v_cache,
            k_scale=self.k_scale,
            v_scale=self.v_scale,
            flash_layout=False,
        )
        query, key, value, k_cache, v_cache, k_scale, v_scale = result

        num_decodes = attn_metadata.plugin_metadata.num_decodes
        num_prefills = attn_metadata.plugin_metadata.num_prefills
        num_extends = attn_metadata.plugin_metadata.num_extends

        num_decode_tokens = attn_metadata.plugin_metadata.num_decode_tokens
        num_extend_tokens = attn_metadata.plugin_metadata.num_extend_tokens

        # calculate for prefills
        if num_prefills > 0:
            assert attn_metadata.plugin_metadata.prefill_metadata is not None

            # prefill part is after decode and extend
            prefill_query = query[num_decode_tokens + num_extend_tokens :]
            prefill_key = key[num_decode_tokens + num_extend_tokens :]
            prefill_value = value[num_decode_tokens + num_extend_tokens :]

            sliding_window = (
                (self.sliding_window, 0, 0)
                if self.sliding_window is not None
                else (-1, -1, 0)
            )

            aiter.flash_attn_varlen_func(
                q=prefill_query,
                k=prefill_key,
                v=prefill_value,
                cu_seqlens_q=attn_metadata.plugin_metadata.prefill_metadata.query_start_loc,
                cu_seqlens_k=attn_metadata.plugin_metadata.prefill_metadata.query_start_loc,
                max_seqlen_q=attn_metadata.plugin_metadata.prefill_metadata.max_query_len,
                max_seqlen_k=attn_metadata.plugin_metadata.prefill_metadata.max_seq_len,
                min_seqlen_q=1,
                dropout_p=attn_metadata.dropout_p,
                softmax_scale=self.scale,
                causal=True,
                window_size=sliding_window,
                alibi_slopes=None,
                sink_ptr=self.sinks,
                out=output_actual_tokens[num_decode_tokens + num_extend_tokens :],
            )

        # calculate for extends
        if num_extends > 0:
            num_blocks, block_size, num_kv_heads, head_size = k_cache.shape
            x = 16 // k_cache.element_size()
            k_cache_template = torch.empty(
                [num_blocks, num_kv_heads, head_size // x, block_size, x],
                dtype=k_cache.dtype,
                device="meta",
            )
            v_cache_template = torch.empty(
                [num_blocks, num_kv_heads, head_size, block_size],
                dtype=v_cache.dtype,
                device="meta",
            )
            new_key_cache = k_cache.view_as(k_cache_template)
            new_value_cache = v_cache.view_as(v_cache_template)
            assert attn_metadata.plugin_metadata.extend_metadata is not None
            extend_tokens_slice = slice(
                num_decode_tokens, num_decode_tokens + num_extend_tokens
            )
            extend_querys = query[extend_tokens_slice]
            extend_keys = key[extend_tokens_slice]
            extend_values = value[extend_tokens_slice]
            extend_outputs = output[extend_tokens_slice]
            extend_block_table = attn_metadata.plugin_metadata.block_table[
                extend_tokens_slice
            ]
            extend_slot_mapping = attn_metadata.plugin_metadata.slot_mapping[
                extend_tokens_slice
            ]
            self.extend_forward(
                attn_metadata=attn_metadata,
                query=extend_querys,
                key=extend_keys,
                value=extend_values,
                key_cache=new_key_cache,
                value_cache=new_value_cache,
                output=extend_outputs,
                cu_seqlens_q=attn_metadata.plugin_metadata.extend_metadata.query_start_loc,
                max_seqlen_q=attn_metadata.plugin_metadata.extend_metadata.max_query_len,
                max_seqlen_k=attn_metadata.plugin_metadata.extend_metadata.max_seq_len,
                min_seqlen_q=1,
                block_table=extend_block_table,
                slot_mapping=extend_slot_mapping,
                k_scale=k_scale,
                v_scale=v_scale,
            )

        # calculate for decodes
        if num_decodes > 0:
            assert attn_metadata.plugin_metadata.decode_metadata is not None

            if _is_fp8_kv_cache(self.kv_cache_dtype):
                # fp8: kv_cache was written in shuffle layout, use shuffle-layout kernels
                num_blocks, block_size, num_kv_heads, head_size = k_cache.shape
                x = 16 // k_cache.element_size()
                k_cache_template = torch.empty(
                    [num_blocks, num_kv_heads, head_size // x, block_size, x],
                    dtype=k_cache.dtype,
                    device="meta",
                )
                v_cache_template = torch.empty(
                    [num_blocks, num_kv_heads, head_size, block_size],
                    dtype=v_cache.dtype,
                    device="meta",
                )
                new_key_cache = k_cache.view_as(k_cache_template)
                new_value_cache = v_cache.view_as(v_cache_template)

                if not hasattr(self, '_debug_decode_logged'):
                    logger.warning(
                        f"FP8 decode: k_cache.shape={new_key_cache.shape}, v_cache.shape={new_value_cache.shape}, "
                        f"k_scale.shape={k_scale.shape if k_scale is not None else None}, "
                        f"v_scale.shape={v_scale.shape if v_scale is not None else None}, "
                        f"use_triton_attn={self.use_triton_attn}, num_decodes={num_decodes}"
                    )
                    self._debug_decode_logged = True

                if self.use_triton_attn:
                    self.paged_attention_triton_plugin_mode(
                        q=query[:num_decode_tokens],
                        k_cache=new_key_cache,
                        v_cache=new_value_cache,
                        k_scale=k_scale,
                        v_scale=v_scale,
                        out=output_actual_tokens[:num_decode_tokens],
                        attn_metadata=attn_metadata,
                    )
                elif num_decodes == _QWEN_GLUON_PA_DECODE_BS:
                    self.paged_attention_triton_plugin_mode(
                        q=query[:num_decode_tokens],
                        k_cache=new_key_cache,
                        v_cache=new_value_cache,
                        k_scale=k_scale,
                        v_scale=v_scale,
                        out=output_actual_tokens[:num_decode_tokens],
                        attn_metadata=attn_metadata,
                    )
                else:
                    self.paged_attention_asm_plugin_mode(
                        q=query[:num_decode_tokens],
                        k_cache=new_key_cache,
                        v_cache=new_value_cache,
                        k_scale=k_scale,
                        v_scale=v_scale,
                        num_decodes=num_decodes,
                        num_decode_tokens=num_decode_tokens,
                        out=output_actual_tokens[:num_decode_tokens],
                        attn_metadata=attn_metadata,
                    )
            else:
                # bf16: kv_cache was written in shuffle layout by rope_cache_plugin_mode,
                # create ATOM-format views for the decode kernels.
                num_blocks_d, block_size_d, num_kv_heads_d, head_size_d = k_cache.shape
                x_d = 16 // k_cache.element_size()
                k_cache_template_d = torch.empty(
                    [num_blocks_d, num_kv_heads_d, head_size_d // x_d, block_size_d, x_d],
                    dtype=k_cache.dtype,
                    device="meta",
                )
                v_cache_template_d = torch.empty(
                    [num_blocks_d, num_kv_heads_d, head_size_d, block_size_d],
                    dtype=v_cache.dtype,
                    device="meta",
                )
                new_key_cache_d = k_cache.view_as(k_cache_template_d)
                new_value_cache_d = v_cache.view_as(v_cache_template_d)

                if self.use_triton_attn:
                    self.paged_attention_triton_plugin_mode(
                        q=query[:num_decode_tokens],
                        k_cache=new_key_cache_d,
                        v_cache=new_value_cache_d,
                        k_scale=k_scale,
                        v_scale=v_scale,
                        out=output_actual_tokens[:num_decode_tokens],
                        attn_metadata=attn_metadata,
                    )
                else:
                    self.paged_attention_triton_plugin_mode(
                        q=query[:num_decode_tokens],
                        k_cache=new_key_cache_d,
                        v_cache=new_value_cache_d,
                        k_scale=k_scale,
                        v_scale=v_scale,
                        out=output_actual_tokens[:num_decode_tokens],
                        attn_metadata=attn_metadata,
                    )

        output = output.view(-1, self.num_heads * self.head_dim)
        return output


def PagedAttentionImplDecoratorForPluginMode(cls):
    method_names = [
        "process_weights_after_loading",
        "rope_cache_plugin_mode",
        "paged_attention_triton_plugin_mode",
        "paged_attention_asm_plugin_mode",
        "paged_attention_flash_plugin_mode",
        "extend_for_sliding_window",
        "extend_forward",
        "forward_impl_plugin_mode",
    ]

    logger.info(
        "Use PagedAttentionImplDecoratorForPluginMode to decorate PagedAttentionImpl"
    )

    # Add all methods to the target class
    for method_name in method_names:
        method = getattr(PagedAttentionImplPluginModeMethods, method_name)
        setattr(cls, method_name, method)

    return cls
