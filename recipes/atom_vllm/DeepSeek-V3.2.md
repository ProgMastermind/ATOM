# DeepSeek-V3.2 with ATOM vLLM Plugin Backend

This recipe shows how to run `deepseek-ai/DeepSeek-V3.2` with the ATOM vLLM plugin backend. For background on the plugin backend, see [ATOM vLLM Plugin Backend](../../docs/vllm_plugin_backend_guide.md).

DeepSeek-V3.2 features sparse MLA and is architecturally similar to GLM-5. The ATOM vLLM plugin backend keeps the standard vLLM CLI, server APIs, and general usage flow compatible with upstream vLLM. For general server options and API usage, users can refer to the [official vLLM documentation](https://docs.vllm.ai/en/latest/).

## Step 1: Pull the OOT Docker

```bash
docker pull rocm/atom-dev:vllm-latest
```

## Step 2: Launch vLLM Server

### TP4

Use this command to launch DeepSeek-V3.2 with tensor parallel size 4.

```bash
vllm serve deepseek-ai/DeepSeek-V3.2 \
    --host localhost \
    --port 8000 \
    --tensor-parallel-size 4 \
    --kv-cache-dtype auto \
    --gpu_memory_utilization 0.9 \
    --async-scheduling \
    --load-format fastsafetensors \
    --compilation-config '{"cudagraph_mode": "FULL_AND_PIECEWISE"}' \
    --trust-remote-code \
    --no-enable-prefix-caching \
    --block-size 1 \
    --max-num-batched-tokens 16384 \
    --max-model-len 16384
```

### TP8

Use this command to launch DeepSeek-V3.2 with tensor parallel size 8.

```bash
vllm serve deepseek-ai/DeepSeek-V3.2 \
    --host localhost \
    --port 8000 \
    --tensor-parallel-size 8 \
    --kv-cache-dtype fp8 \
    --gpu_memory_utilization 0.9 \
    --async-scheduling \
    --load-format fastsafetensors \
    --compilation-config '{"cudagraph_mode": "FULL_AND_PIECEWISE"}' \
    --trust-remote-code \
    --no-enable-prefix-caching \
    --block-size 1 \
    --max-num-batched-tokens 16384 \
    --max-model-len 16384
```

## Step 3: Performance Benchmark

Users can use the default vLLM bench commands for performance benchmarking.

```bash
vllm bench serve \
    --host localhost \
    --port 8000 \
    --model deepseek-ai/DeepSeek-V3.2 \
    --dataset-name random \
    --random-input-len 8000 \
    --random-output-len 1000 \
    --random-range-ratio 0.8 \
    --max-concurrency 64 \
    --num-prompts 640 \
    --trust-remote-code \
    --ignore-eos \
    --percentile-metrics ttft,tpot,itl,e2el
```

### Optional: Enable Profiling

If you want to collect profiling trace, you can use the same API as default vLLM to add `--profiler-config "$profiler_config"` to the `vllm serve` command above.

```bash
profiler_config=$(printf '{"profiler":"torch","torch_profiler_dir":"%s","torch_profiler_with_stack":true,"torch_profiler_record_shapes":true}' \
    "${your-profiler-dir}")
```

## Step 4: Accuracy Validation

The sparse MLA mechanism contains an indexer that selects the top-k tokens it deems most relevant for each query from the KV cache. To evaluate its accuracy, it is recommended to use requests with long enough context so that the indexer can be tested. In `lm_eval`, this can be set by increasing `num_fewshot` to increase the context length.

```bash
lm_eval --model local-completions \
        --model_args model=deepseek-ai/DeepSeek-V3.2,base_url=http://localhost:8000/v1/completions,num_concurrent=64,max_retries=3 \
        --tasks gsm8k \
        --num_fewshot 20
```
