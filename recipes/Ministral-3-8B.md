# Ministral-3-8B-Instruct-2512 on gfx1201 (RX 9070 XT)

This recipe describes running `mistralai/Ministral-3-8B-Instruct-2512`
(natively FP8 trained) on a single RDNA4 GPU using ATOM's
`TORCH_NATIVE_ATTENTION` backend. The backend is selected automatically
when ATOM detects gfx1201; on other archs it does nothing.

## Why not the default AITER path?

The AITER package shipped in `rocm/atom-dev:latest` ships prebuilt HIP
`.so` files only for gfx94x/95x. Loading any of those modules on
gfx1201 segfaults with `No compatible code objects found for: gfx1201`.
The torch-native backend bypasses the prebuilt path:

| Op | Backend on gfx1201 |
|---|---|
| Paged attention prefill + decode | `F.scaled_dot_product_attention` per-seq |
| KV cache write | torch `index_copy_` on a `[num_blocks, block_size, kv_heads, d]` slab |
| RMSNorm (with/without residual) | torch RMSNorm fallback |
| Per-tensor FP8 GEMM (qkv/o/gate_up/down proj) | dequant FP8 → BF16 → `F.linear` |
| SiLU + Mul (SwiGLU) | `forward_native` (existing torch path) |
| Mixed Gumbel sampler | torch Gumbel-max + argmax |
| YaRN-scaled RoPE | `forward_native` via env var |

## Required env vars

```bash
export ATOM_USE_TRITON_GEMM=1
export AITER_LOG_LEVEL=WARNING
export AITER_ROPE_NATIVE_BACKEND=1
export ATOM_LLAMA_ENABLE_AITER_TRITON_FUSED_RMSNORM_QUANT=0
export ATOM_LLAMA_ENABLE_AITER_TRITON_FUSED_SILU_MUL_QUANT=0
export ATOM_ENABLE_ALLREDUCE_RMSNORM_FUSION=0
```

## Required CLI flags

* `--enforce-eager --level 0` — CUDAGraph capture is not yet supported
  by the torch-native backend.
* `--kv_cache_dtype bf16` — FP8 KV is a TODO; only BF16 is wired up.
* `-tp 1` — multi-GPU TP not exercised against this backend yet.

## Smoke test

```bash
python3 -m atom.examples.simple_inference \
  --model /path/to/Ministral-3-8B-Instruct-2512 \
  --enforce-eager --level 0 -tp 1 --kv_cache_dtype bf16 \
  --max-model-len 4096 --max-tokens 32 \
  --gpu-memory-utilization 0.85
```

## OpenAI-compatible server

```bash
python3 -m atom.entrypoints.openai_server \
  --model /path/to/Ministral-3-8B-Instruct-2512 \
  --enforce-eager --level 0 --kv_cache_dtype bf16 \
  --max-model-len 4096 \
  --server-port 30000
```

## gsm8k via lm_eval (5-shot, generate-until)

```bash
OPENAI_API_KEY=dummy lm_eval \
  --model local-completions \
  --model_args model=/path/to/Ministral-3-8B-Instruct-2512,base_url=http://localhost:30000/v1/completions,tokenizer=/path/to/Ministral-3-8B-Instruct-2512,tokenized_requests=False,max_length=4096,num_concurrent=2 \
  --tasks gsm8k --num_fewshot 5 --batch_size 1
```

### Verified results on RX 9070 XT (gfx1201, 16 GB)

| Setup | n | Accuracy |
|---|---:|---:|
| gsm8k strict-match, n=5 limit | 5 | 0.80 |
| gsm8k strict-match, n=20 limit | 20 | 0.60 |

Throughput on this backend: TPOT ~0.28 s/token (slow — pure-torch decode,
GQA expansion + Python loop per request). Full gsm8k (1319 problems)
extrapolates to ~12 hours single-stream; concurrent=2 roughly halves it.
A future Triton flash-attention drop-in is the obvious next step.

## Known caveats

* 238 `activation_scale` checkpoint tensors are silently dropped during
  load. Harmless because the FP8 GEMM fallback dequantizes weights to
  BF16 and ignores per-channel input scale, but worth fixing if FP8
  native compute ever lands.
* `compute_block_bytes` reports a placeholder pool size. The KV pool is
  allocated correctly but the engine logs a 100% mismatch warning at
  boot. Cosmetic — KV writes/reads work end-to-end.
* `--max-model-len` must accommodate the chat-templated prompt (the
  Mistral system prompt is ~540 tokens).
