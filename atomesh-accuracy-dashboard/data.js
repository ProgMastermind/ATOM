window.BENCHMARK_DATA = {
  "lastUpdate": 1781511616177,
  "repoUrl": "https://github.com/ROCm/ATOM",
  "entries": {
    "Benchmark": [
      {
        "commit": {
          "author": {
            "email": "wanzhenchn@gmail.com",
            "name": "wanzhenchn",
            "username": "wanzhenchn"
          },
          "committer": {
            "email": "noreply@github.com",
            "name": "GitHub",
            "username": "web-flow"
          },
          "distinct": true,
          "id": "18b17f4043ca381da8d1c8ec1beb409b44353b2a",
          "message": "ci(mesh): add Atomesh accuracy and benchmark workflows (#1159)\n\n* ci(mesh): add Atomesh accuracy and benchmark workflows\n\n- Validate standalone-mode accuracy via Atomesh entrypoints.\n- Mocker benchmark to PD routing scenarios with topology and consumer concurrency matrix.\n\n* [ci][mesh] add Atomesh mocker benchmark dashboard\n\n- Add a custom dashboard for Atomesh mocker benchmark results.\n- Show throughput, latency, detailed performance data, commit links, and CI run links.\n- Align the benchmark matrix with 1P1D, 2P1D, and 3P1D topologies across consumer concurrency levels.\n\n* [ci] Skip unrelated ATOM, vLLM, and SGLang CI for mesh-only PRs.\n\n* [ci][mesh] Enable mocker dashboard publishing workflow to run on zwan/feat-mesh-ci pushes.\n\n* Polish Atomesh mocker dashboard legends\n\n* [ci][mesh] fix atomesh standalone accuracy data source\n\n* Revert 'Enable mocker dashboard publishing workflow to run on zwan/feat-mesh-ci pushes.'\n\n* [ci][mesh] add logo and display theme for mesh mocker benchmark dashboard\n\n* [ci][mesh] Polish Atomesh dashboard and accuracy data flow",
          "timestamp": "2026-06-15T15:50:22+08:00",
          "tree_id": "6f4740956be82e7177ea5f44dd264b4cbcb4729f",
          "url": "https://github.com/ROCm/ATOM/commit/18b17f4043ca381da8d1c8ec1beb409b44353b2a"
        },
        "date": 1781511608187,
        "tool": "customBiggerIsBetter",
        "benches": [
          {
            "name": "ATOMesh::DeepSeek-R1-0528 accuracy (GSM8K)",
            "value": 0.953,
            "unit": "score",
            "extra": "Run: https://github.com/ROCm/ATOM/actions/runs/27531858784 | Threshold: 0.94 | Baseline: 0.9553 | BaselineModel: deepseek-ai/DeepSeek-R1-0528 | BaselineNote: CI measured FP8 baseline (GSM8K 3-shot flexible-extract) | Docker: rocm/atom-dev:nightly_202606141623 | GPU: AMD Radeon Graphics | VRAM: 288GB | ROCm: 7.2.4 | strict-match: 0.9492 | fewshot: 3 | Model: /models/deepseek-ai/DeepSeek-R1-0528"
          },
          {
            "name": "ATOMesh::Meta-Llama-3-8B-Instruct accuracy (GSM8K)",
            "value": 0.7483,
            "unit": "score",
            "extra": "Run: https://github.com/ROCm/ATOM/actions/runs/27531858784 | Threshold: 0.73 | Baseline: 0.75 | BaselineModel: meta-llama/Meta-Llama-3-8B-Instruct | BaselineNote: HF reports 0.796 but 8-shot CoT; CI uses 3-shot, not comparable | Docker: rocm/atom-dev:nightly_202606141623 | GPU: AMD Instinct MI355X | VRAM: 252GB | ROCm: 7.2.4 | strict-match: 0.7491 | fewshot: 3 | Model: /models/meta-llama/Meta-Llama-3-8B-Instruct"
          }
        ]
      }
    ]
  }
}