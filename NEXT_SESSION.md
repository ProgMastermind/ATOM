# Next-session pickup notes — ATOM gfx1201 / Ministral-3

## What runs today (commit 4f848a9 on branch `carhuang/support_gfx1201_mistral3`)

```bash
ssh -i /home/carhuang/id_rsa_carhuang carhuang@agent-tr9980x-01
docker exec -it atom_gfx1201 bash -lc 'cd /tmp && \
  ATOM_USE_TRITON_GEMM=1 AITER_LOG_LEVEL=WARNING \
  python3 -m atom.examples.simple_inference \
    --model /mnt/sda1/carhuang/models/Ministral-3-8B-Instruct-2512 \
    --enforce-eager --level 0 -tp 1 --kv_cache_dtype bf16 \
    --max-model-len 1024 --max-tokens 4 \
    --gpu-memory-utilization 0.85'
```

Reaches: `Model load done` → `TorchNativeMetadataBuilder: initialized` → SIGSEGV in
`ModelRunner.warmup_model()` (model_runner.py:666). The first forward pass
exercises every aiter HIP kernel in the attention + KV-cache + RMSNorm path; one
of them lacks a gfx1201 code object.

## Key paths / context

* Repo: `/mnt/sda1/carhuang/repo/ATOM` (editable installed in container)
* Branch: `carhuang/support_gfx1201_mistral3`
* Model: `/mnt/sda1/carhuang/models/Ministral-3-8B-Instruct-2512`
* Container: `atom_gfx1201` (always-running on `agent-tr9980x-01`)
* Aiter source: `/app/aiter-test/aiter/` (matches commit 247e9b1 of ATOM)
* Plan doc: `/home/carhuang/.claude/plans/glittery-dazzling-crayon.md`
* Scaffold: `atom/model_ops/attentions/torch_native_attn.py` (TODOs in module docstring)

## Find which aiter HIP load fails next

```bash
docker exec atom_gfx1201 bash -lc '
  cd /tmp && rm -rf /root/.cache/atom/* && \
  ATOM_USE_TRITON_GEMM=1 AITER_LOG_LEVEL=WARNING \
  AMD_LOG_LEVEL=4 \
  python3 -m atom.examples.simple_inference \
    --model /mnt/sda1/carhuang/models/Ministral-3-8B-Instruct-2512 \
    --enforce-eager --level 0 -tp 1 --kv_cache_dtype bf16 \
    --max-model-len 1024 --max-tokens 4 \
    --gpu-memory-utilization 0.85 > /tmp/atom_run.log 2>&1
  # find what loaded right before the crash:
  grep -nB 5 "No compatible code" /tmp/atom_run.log | tail -40
  # or trace which python frame:
  awk "NR<=NR_OF_FAILURE && !/^:[0-9]:/" /tmp/atom_run.log | tail -30'
```

## TODO order (smallest blast radius first)

1. **TODO-3 / TODO-4 (impl):** the warmup forward goes through PagedAttentionImpl
   today (selector returns TorchNativeBackend, so `get_impl_cls` returns
   TorchNativeAttentionImpl). But maybe ops.Attention is still PagedAttention.
   Confirm by adding a `print(f"impl class: {type(self.attn)}")` in
   LlamaAttention.__init__. If it's still PagedAttentionImpl, that's why we
   hit aiter — the impl swap isn't happening yet.
2. **Implement `TorchNativeAttentionImpl.__init__` for real (TODO-3)** — copy
   the field set from `attention_mha.py:PagedAttentionImpl.__init__` (lines
   29–90) minus aiter-specific stuff; just store fields and let kv cache get
   set later via attribute assignment (model_runner does `module.k_cache = ...`).
3. **Implement `TorchNativeAttentionImpl.forward` minimally** —
   prefill: `F.scaled_dot_product_attention(q, k, v, is_causal=True)` per-seq.
   For first usable version, accept that this is slow and not paged — just
   correctness. Decode: gather K/V from cache by slot_mapping → SDPA.
4. **TODO-7 KV cache write** — replace any `aiter.reshape_and_cache` call with
   `cache.view(num_blocks, block_size, ...).index_put_(slot_mapping, kv)`.
5. **TODO-1/2 metadata** — only matters once impl actually consumes them
   (currently both raise NotImplementedError).
6. RMSNorm fallback — likely needed if ATOM_LLAMA_ENABLE_AITER_TRITON_FUSED_RMSNORM_QUANT=0
   doesn't already route through torch.

## Watch out

* `--enforce-eager --level 0` are required until CUDAGraph capture works
  through the new backend.
* `kv_cache_dtype=bf16` only — FP8 KV path is TODO-8.
* The `238 activation_scale tensors silently dropped` warning is a separate
  small bug (Mistral's per-q/k/v static activation scale doesn't merge into
  ATOM's fused `qkv_proj.input_scale`). Likely degrades FP8 accuracy but
  not the blocker.

## Memory entries to consider saving

* That ATOM at commit 247e9b1 is what's compatible with the aiter shipped
  in `rocm/atom-dev:latest` (newer ATOM HEAD requires `aiter.ops.shuffle.shuffle_scale`
  which the baked aiter doesn't have).
* That aiter's source officially supports gfx1201 (in GPU_ARCHS allowlist) —
  rebuild path is `cd /app/aiter-test && GPU_ARCHS=gfx1201 pip install -e .`
  (~30–60 min). Kept in reserve as plan B.
