# Next-session pickup notes — ATOM gfx1201 / Ministral-3

## What runs today (commit `c983d98` on branch `carhuang/support_gfx1201_mistral3`)

```bash
ssh -i /home/carhuang/id_rsa_carhuang carhuang@agent-tr9980x-01
docker exec -it atom_gfx1201 bash -lc '
  cd /tmp && \
  ATOM_USE_TRITON_GEMM=1 AITER_LOG_LEVEL=WARNING \
  ATOM_LLAMA_ENABLE_AITER_TRITON_FUSED_RMSNORM_QUANT=0 \
  ATOM_LLAMA_ENABLE_AITER_TRITON_FUSED_SILU_MUL_QUANT=0 \
  ATOM_ENABLE_ALLREDUCE_RMSNORM_FUSION=0 \
  python3 -m atom.examples.simple_inference \
    --model /mnt/sda1/carhuang/models/Ministral-3-8B-Instruct-2512 \
    --enforce-eager --level 0 -tp 1 --kv_cache_dtype bf16 \
    --max-model-len 256 --max-tokens 4 \
    --gpu-memory-utilization 0.85'
```

How far it gets right now (with probes removed, you'll just see SIGSEGV):

```
Model load done
TorchNativeMetadataBuilder: initialized
ModelRunner.forward → prepare_model → run_model
  embed → ✓
  layer 0 → input_layernorm (RMSNorm via torch fallback ✓)
          → self_attn → qkv_proj → SIGSEGV  ← next blocker
```

## Next blocker: FP8 GEMM in `qkv_proj` / `gate_up_proj` / `down_proj` / `o_proj`

Mistral-3 weights are FP8 per-tensor (`weight_block_size: null`). When ATOM's
`linear.py` runs the GEMM, it picks one of the prebuilt aiter HIP kernels:
`aiter.gemm_a8w8`, `aiter.gemm_a8w8_bpreshuffle`, or `aiter.gemm_a8w8_blockscale`.
None of these have a gfx1201 code object.

`ATOM_USE_TRITON_GEMM=1` only swaps in the **blockscale** Triton kernel
(`aiter.ops.triton.gemm.basic.gemm_a8w8_blockscale`), which doesn't help
per-tensor FP8.

Two reasonable directions for next session:

### Option A — torch fallback (mirrors the RMSNorm fix done this session)

Patch `atom/model_ops/linear.py` to detect gfx1201 and dequantize FP8 → BF16
inside the linear forward, then `torch.matmul(input_bf16, weight_bf16.T)`.
Slow but correct. Pattern to copy from the RMSNorm fallback:

```python
# atom/model_ops/layernorm.py:_is_gfx1201_layernorm + _rmsnorm_torch
```

The relevant linear-layer call sites are inside `linear.py`'s
`weight_loader_process` / forward methods — the FP8 GEMM dispatch is around
the `gemm_a8w8*` calls. Dequant approach: `weight_bf16 = (weight_fp8.to(torch.float32) * weight_scale).to(torch.bfloat16)`.

### Option B — dequantize the model at load time (simpler globally)

Find where ATOM stores FP8 weights post-load and add a one-time dequant
sweep when on gfx1201 so the rest of ATOM thinks it's a BF16 model.
HF's transformers has `FineGrainedFP8Config(dequantize=True)` doing
exactly this; mirror the idea inside ATOM. Trades VRAM (12GB → ~17GB
weights) for a one-shot fix that bypasses the FP8-kernel ecosystem
entirely. Won't fit on 16 GB without offload.

**Recommendation:** Option A — tighter scope, reuses the RMSNorm pattern,
keeps weights in FP8 (preserves the user's FP8 goal).

## After FP8 GEMM works, more aiter HIP loads will surface

In rough order of likelihood (each will SIGSEGV the same way):

1. **`silu_and_mul`** in `atom/model_ops/activation.py` — used by SwiGLU MLP.
   Trivial torch fallback: `F.silu(x[..., :n//2]) * x[..., n//2:]`.
2. **`reshape_and_cache`** for KV writes when our impl tries to fill the
   paged cache. We're skipping the paged cache today, so this only matters
   once we add decode (TODO-7).
3. **Anything else in the model_ops/ files that imports aiter's prebuilt
   modules.** Strategy: each one gets a `_is_gfx1201()`-gated torch
   fallback at the call site. Don't try to refactor — just bisect by
   re-running and patching the next thing that crashes.

## Useful test loop

Re-add probes any time by running `/tmp/probe_llama.py` (kept on the box)
before a run; revert with `git checkout -- atom/models/llama.py atom/model_engine/model_runner.py`
after.

## Critical paths reminder

| Purpose | File |
|---|---|
| Branch | `carhuang/support_gfx1201_mistral3` (local on remote, not pushed) |
| Working RMSNorm fallback (template for next ones) | `atom/model_ops/layernorm.py:_is_gfx1201_layernorm` |
| Backend selector | `atom/utils/selector.py:get_attn_backend_cls` |
| Torch-native impl (prefill done, decode TODO) | `atom/model_ops/attentions/torch_native_attn.py` |
| Dispatch hook | `atom/model_ops/paged_attention.py` (TORCH_NATIVE_ATTENTION branch) |
| Mistral3 model port | `atom/models/mistral3.py` |
| Plan doc | `~/.claude/plans/glittery-dazzling-crayon.md` (host-side) |

## Required env vars to repro current furthest progress

```
ATOM_USE_TRITON_GEMM=1                                  # blockscale Triton GEMM (best-effort)
AITER_LOG_LEVEL=WARNING                                 # quiet
ATOM_LLAMA_ENABLE_AITER_TRITON_FUSED_RMSNORM_QUANT=0    # don't try the FP8-fused RMSNorm path
ATOM_LLAMA_ENABLE_AITER_TRITON_FUSED_SILU_MUL_QUANT=0
ATOM_ENABLE_ALLREDUCE_RMSNORM_FUSION=0
```

CLI required: `--enforce-eager --level 0 --kv_cache_dtype bf16` (CUDAGraph
capture and FP8 KV are both still TODO).
