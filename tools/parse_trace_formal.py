#!/usr/bin/env python3
"""Small, step-by-step ATOM trace parser.

Step 1: find the decode warmup window in the capture trace that corresponds to
the first decode event in the run trace.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import json
import os
import re
from glob import glob
from typing import Any


SPECIAL_KERNEL_LAUNCH_NAMES = {"hipmemcpyasync"}


def load_events(path: str) -> list[dict[str, Any]]:
    opener = gzip.open if path.endswith(".gz") else open
    with opener(path, "rt", encoding="utf-8", errors="replace") as f:
        return json.load(f).get("traceEvents", [])


def event_end(event: dict[str, Any]) -> float:
    return float(event.get("ts", 0.0)) + float(event.get("dur", 0.0))


def is_kernel_launch(name: str) -> bool:
    normalized = name.lower()
    return (
        "launch" in normalized and "kernel" in normalized
    ) or normalized in SPECIAL_KERNEL_LAUNCH_NAMES


def short(text: Any, limit: int = 80) -> str:
    value = str(text)
    return value if len(value) <= limit else value[: limit - 3] + "..."


def model_name_from_trace(path: str) -> str | None:
    base = os.path.basename(path)
    if "_ts_" not in base:
        return None
    prefix = base.split("_ts_", 1)[0]
    if prefix.startswith("capture_graph_"):
        prefix = prefix[len("capture_graph_") :]
    return prefix or None


def find_capture_trace(run_trace: str) -> str | None:
    model_name = model_name_from_trace(run_trace)
    if not model_name:
        return None
    trace_dir = os.path.dirname(run_trace) or "."
    pattern = os.path.join(trace_dir, f"capture_graph_{model_name}_ts_*.pt.trace.json*")
    candidates = sorted(glob(pattern), key=os.path.getmtime, reverse=True)
    run_abs = os.path.abspath(run_trace)
    for candidate in candidates:
        if os.path.abspath(candidate) != run_abs:
            return candidate
    return None


def find_first_decode(events: list[dict[str, Any]]) -> dict[str, Any]:
    decodes = sorted(
        [
            event
            for event in events
            if event.get("ph") == "X"
            and event.get("cat") == "gpu_user_annotation"
            and str(event.get("name", "")).startswith("decode[")
        ],
        key=lambda event: event["ts"],
    )
    if not decodes:
        raise RuntimeError("No decode gpu_user_annotation found in run trace.")
    return decodes[0]


def decode_batch_size(decode_event: dict[str, Any]) -> int:
    match = re.search(r"bs=(\d+)", str(decode_event.get("name", "")))
    if not match:
        raise RuntimeError(f"Could not parse batch size from {decode_event.get('name')!r}")
    return int(match.group(1))


def find_cpu_capture_graphs(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        [
            event
            for event in events
            if event.get("ph") == "X"
            and event.get("cat") == "user_annotation"
            and str(event.get("name", "")).startswith("capture_graph_bs_")
        ],
        key=lambda event: event["ts"],
    )


def find_capture_graph_for_bs(
    capture_events: list[dict[str, Any]], batch_size: int
) -> dict[str, Any]:
    graphs = find_cpu_capture_graphs(capture_events)
    if not graphs:
        raise RuntimeError("No CPU capture_graph_bs_* annotations found in capture trace.")
    target_name = f"capture_graph_bs_{batch_size}"
    for graph in graphs:
        if graph.get("name") == target_name:
            return graph
    raise RuntimeError(f"No {target_name} found in capture trace.")


def warmup_window_for_graph(
    capture_events: list[dict[str, Any]], target_graph: dict[str, Any]
) -> tuple[float, float]:
    """Return [previous_capture_graph_end, target_capture_graph_start)."""
    start = 0.0
    for graph in find_cpu_capture_graphs(capture_events):
        if graph is target_graph:
            return start, float(target_graph["ts"])
        start = max(start, event_end(graph))
    raise RuntimeError("Target capture graph was not in capture graph list.")


def count_events_in_window(
    events: list[dict[str, Any]], start: float, end: float
) -> dict[str, int]:
    counts = {"duration": 0, "user_annotation": 0, "cuda_runtime": 0, "kernel": 0}
    for event in events:
        if event.get("ph") != "X":
            continue
        ts = float(event.get("ts", 0.0))
        if not (start <= ts < end):
            continue
        counts["duration"] += 1
        cat = event.get("cat")
        if cat in counts:
            counts[cat] += 1
    return counts


def build_correlation_index(
    events: list[dict[str, Any]], start: float, end: float
) -> tuple[dict[Any, dict[str, Any]], dict[Any, dict[str, Any]]]:
    launches: dict[Any, dict[str, Any]] = {}
    kernels: dict[Any, dict[str, Any]] = {}
    for event in events:
        if event.get("ph") != "X":
            continue
        ts = float(event.get("ts", 0.0))
        if not (start <= ts < end):
            continue
        corr = (event.get("args") or {}).get("correlation")
        if corr is None:
            continue
        if event.get("cat") == "cuda_runtime" and is_kernel_launch(
            str(event.get("name", ""))
        ):
            launches.setdefault(corr, event)
        elif event.get("cat") == "kernel":
            kernels.setdefault(corr, event)
    return launches, kernels


def containing_annotations(
    event: dict[str, Any], annotations: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    start = float(event["ts"])
    end = event_end(event)
    return [
        ann
        for ann in annotations
        if ann.get("pid") == event.get("pid")
        and ann.get("tid") == event.get("tid")
        and float(ann.get("ts", 0.0)) <= start
        and end <= event_end(ann)
    ]


def is_compiled_graph_tag(name: str) -> bool:
    return name.startswith("## Call CompiledFxGraph")


def cpu_fallback_tag_for_compiled_kernel(
    kernel: dict[str, Any],
    launch_by_corr: dict[Any, dict[str, Any]],
    cpu_events: list[dict[str, Any]],
) -> str | None:
    corr = (kernel.get("args") or {}).get("correlation")
    launch = launch_by_corr.get(corr)
    if launch is None:
        return None

    containers = containing_annotations(launch, cpu_events)
    # Include cpu_op parents as well as user annotations, then pick the largest
    # non-CompiledFxGraph container. This maps tiny compiled graph kernels such
    # as FillFunctor copies back to semantic CPU ops like aiter::all_reduce_.
    start = float(launch["ts"])
    end = event_end(launch)
    containers.extend(
        event
        for event in cpu_events
        if event.get("cat") == "cpu_op"
        and event.get("pid") == launch.get("pid")
        and event.get("tid") == launch.get("tid")
        and float(event.get("ts", 0.0)) <= start
        and end <= event_end(event)
    )
    candidates = [
        event
        for event in containers
        if not is_compiled_graph_tag(str(event.get("name", "")))
    ]
    if not candidates:
        return None
    return str(max(candidates, key=lambda event: float(event.get("dur", 0.0))).get("name"))


def gpu_tag_for_kernel(
    kernel: dict[str, Any],
    gpu_annotations: list[dict[str, Any]],
    launch_by_corr: dict[Any, dict[str, Any]],
    cpu_events: list[dict[str, Any]],
) -> str:
    containers = containing_annotations(kernel, gpu_annotations)
    if not containers:
        return "<no gpu tag>"
    tag = str(min(containers, key=lambda event: float(event.get("dur", 0.0))).get("name"))
    if is_compiled_graph_tag(tag):
        fallback = cpu_fallback_tag_for_compiled_kernel(kernel, launch_by_corr, cpu_events)
        if fallback:
            return fallback
    return tag


def build_warmup_mapping(
    capture_events: list[dict[str, Any]], start: float, end: float
) -> list[dict[str, Any]]:
    """Build the internal decode warmup mapping.

    Each row is intentionally minimal:
      - module: resolved CPU/GPU tag name
      - kernel: GPU kernel name
      - stream: GPU stream id

    This mapping is the attribution source for later replay-duration matching;
    it is not meant to be emitted as the final user-facing breakdown.
    """
    launch_by_corr, _ = build_correlation_index(capture_events, start, end)
    cpu_events = [
        event
        for event in capture_events
        if event.get("ph") == "X"
        and start <= float(event.get("ts", 0.0)) < end
        and event.get("cat") in {"user_annotation", "cpu_op"}
    ]
    gpu_annotations = [
        event
        for event in capture_events
        if event.get("ph") == "X"
        and start <= float(event.get("ts", 0.0)) < end
        and event.get("cat") == "gpu_user_annotation"
    ]
    kernels = sorted(
        [
            event
            for event in capture_events
            if event.get("ph") == "X"
            and start <= float(event.get("ts", 0.0)) < end
            and event.get("cat") == "kernel"
        ],
        key=lambda event: event["ts"],
    )
    mapping: list[dict[str, Any]] = []
    for kernel in kernels:
        args = kernel.get("args") or {}
        mapping.append(
            {
                "module": gpu_tag_for_kernel(
                    kernel, gpu_annotations, launch_by_corr, cpu_events
                ),
                "kernel": str(kernel.get("name", "")),
                "stream": args.get("stream"),
            }
        )
    return mapping


def print_first_warmup_mappings(
    mapping: list[dict[str, Any]], limit: int
) -> None:
    print("")
    print(f"First {limit} warmup mappings:")
    print("| # | module/tag | stream | kernel |")
    print("|---:|---|---:|---|")
    for idx, item in enumerate(mapping[:limit]):
        print(
            f"| {idx} | `{short(item['module'], 55)}` | {item['stream']} | "
            f"`{short(item['kernel'], 85)}` |"
        )


def decode_gpu_window(
    run_events: list[dict[str, Any]], decode_event: dict[str, Any]
) -> tuple[float, float]:
    """Return the GPU annotation time range for the selected decode event.

    We intentionally use the GPU-side annotation range here: the final CSV is
    for observed replay GPU kernels. Kernels that fall just outside this range
    are not included in this first formal path.
    """
    external_id = (decode_event.get("args") or {}).get("External id")
    gpu_decodes = [
        event
        for event in run_events
        if event.get("ph") == "X"
        and event.get("cat") == "gpu_user_annotation"
        and (event.get("args") or {}).get("External id") == external_id
    ]
    if not gpu_decodes:
        # Fallback for traces without GPU annotation projection.
        return float(decode_event["ts"]), event_end(decode_event)
    return min(float(event["ts"]) for event in gpu_decodes), max(
        event_end(event) for event in gpu_decodes
    )


def replay_kernels_in_window(
    run_events: list[dict[str, Any]], start: float, end: float
) -> list[dict[str, Any]]:
    return sorted(
        [
            event
            for event in run_events
            if event.get("ph") == "X"
            and event.get("cat") == "kernel"
            and start <= float(event.get("ts", 0.0)) < end
        ],
        key=lambda event: event["ts"],
    )


def remap_streams(replay_kernels: list[dict[str, Any]]) -> dict[Any, int]:
    """Map real replay stream ids to compact 1..N ids by numeric order."""
    streams = sorted(
        {
            (event.get("args") or {}).get("stream")
            for event in replay_kernels
        },
        key=lambda value: (value is None, value),
    )
    return {stream: idx + 1 for idx, stream in enumerate(streams)}


LAYER_RE = re.compile(r"(^|\.)layers\.(\d+)\.")


def module_layer(module: str) -> int | None:
    match = LAYER_RE.search(module)
    return int(match.group(2)) if match else None


def normalize_layer_module(module: str) -> str:
    return re.sub(r"(^|\.)layers\.\d+\.", r"\1layers.*.", module)


def layer_group_label(layers: list[int]) -> str:
    layers = sorted(layers)
    if not layers:
        return "layers <empty>"
    if len(layers) == 1:
        return f"layer {layers[0]}"
    if layers == list(range(layers[0], layers[-1] + 1)):
        return f"layers {layers[0]}-{layers[-1]}"
    if len(layers) <= 8:
        return "layers " + ",".join(str(layer) for layer in layers)
    return f"layers {layers[0]},{layers[1]},...,{layers[-1]} ({len(layers)} layers)"


def lcs_length(a: list[str], b: list[str]) -> int:
    if not a or not b:
        return 0
    prev = [0] * (len(b) + 1)
    for item_a in a:
        curr = [0] * (len(b) + 1)
        for j, item_b in enumerate(b, start=1):
            if item_a == item_b:
                curr[j] = prev[j - 1] + 1
            else:
                curr[j] = max(prev[j], curr[j - 1])
        prev = curr
    return prev[-1]


def infer_replay_to_warmup_stream_map(
    replay_kernels: list[dict[str, Any]], warmup_mapping: list[dict[str, Any]]
) -> dict[Any, Any]:
    """Infer which warmup stream each replay stream should match against."""
    warmup_by_stream: dict[Any, list[str]] = {}
    replay_by_stream: dict[Any, list[str]] = {}
    for item in warmup_mapping:
        warmup_by_stream.setdefault(item["stream"], []).append(item["kernel"])
    for event in replay_kernels:
        stream = (event.get("args") or {}).get("stream")
        replay_by_stream.setdefault(stream, []).append(str(event.get("name", "")))

    stream_map: dict[Any, Any] = {}
    for replay_stream, replay_names in replay_by_stream.items():
        best_stream = None
        best_score = -1
        best_ratio = -1.0
        for warmup_stream, warmup_names in warmup_by_stream.items():
            score = lcs_length(warmup_names, replay_names)
            ratio = score / max(1, min(len(warmup_names), len(replay_names)))
            if (score, ratio) > (best_score, best_ratio):
                best_score = score
                best_ratio = ratio
                best_stream = warmup_stream
        stream_map[replay_stream] = best_stream
    return stream_map


def match_replay_to_warmup(
    replay_kernels: list[dict[str, Any]], warmup_mapping: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Greedy monotonic match from replay kernels to warmup mapping by stream.

    First infer replay-stream -> warmup-stream from kernel-name sequence
    similarity.  Then each replay stream gets its own cursor inside its mapped
    warmup stream, so kernels from other streams cannot advance the cursor.
    """
    replay_to_warmup_stream = infer_replay_to_warmup_stream_map(
        replay_kernels, warmup_mapping
    )
    warmup_by_stream: dict[Any, list[dict[str, Any]]] = {}
    for item in warmup_mapping:
        warmup_by_stream.setdefault(item["stream"], []).append(item)

    rows: list[dict[str, Any]] = []
    stream_cursors: dict[Any, int] = {}
    for replay in replay_kernels:
        replay_kernel_name = str(replay.get("name", ""))
        replay_stream = (replay.get("args") or {}).get("stream")
        warmup_stream = replay_to_warmup_stream.get(replay_stream)
        warmup_stream_items = warmup_by_stream.get(warmup_stream, [])
        warmup_pos = stream_cursors.get(replay_stream, 0)
        matched: dict[str, Any] | None = None
        for idx in range(warmup_pos, len(warmup_stream_items)):
            if warmup_stream_items[idx]["kernel"] == replay_kernel_name:
                matched = warmup_stream_items[idx]
                stream_cursors[replay_stream] = idx + 1
                break
        rows.append(
            {
                "cpu_module": matched["module"] if matched else "<unmatched>",
                "kernel_name": replay_kernel_name,
                "stream": replay_stream,
                "duration_us": float(replay.get("dur", 0.0)),
            }
        )
    return rows


def build_grouped_breakdown_rows(
    rows: list[dict[str, Any]], stream_map: dict[Any, int]
) -> list[dict[str, Any]]:
    """Aggregate matched replay rows into layer-structure groups.

    Layer rows are grouped by identical per-layer operator sequence.  Each output
    row for a layer group is the average time for that operator position across
    layers in the group.  Non-layer and unmatched rows are aggregated by
    module/kernel/stream.
    """
    layer_rows: dict[int, list[dict[str, Any]]] = {}
    non_layer_accum: dict[tuple[str, str, int], float] = {}
    unmatched_accum: dict[tuple[str, int], float] = {}

    for row in rows:
        stream_no = stream_map.get(row["stream"], 0)
        if row["cpu_module"] == "<unmatched>":
            key = (row["kernel_name"], stream_no)
            unmatched_accum[key] = unmatched_accum.get(key, 0.0) + row["duration_us"]
            continue

        layer = module_layer(row["cpu_module"])
        normalized = normalize_layer_module(row["cpu_module"])
        normalized_row = {
            **row,
            "module_pattern": normalized,
            "stream_no": stream_no,
        }
        if layer is None:
            key = (normalized, row["kernel_name"], stream_no)
            non_layer_accum[key] = non_layer_accum.get(key, 0.0) + row["duration_us"]
        else:
            layer_rows.setdefault(layer, []).append(normalized_row)

    grouped: list[dict[str, Any]] = []

    # Non-layer prologue/epilogue rows.
    for (module, kernel, stream_no), total_us in non_layer_accum.items():
        grouped.append(
            {
                "layer_group": "non_layer",
                "module": module,
                "kernel": kernel,
                "stream_no": stream_no,
                "time_us": total_us,
            }
        )

    # Layer groups by exact normalized operator sequence.
    signature_to_layers: dict[tuple[tuple[str, str, int], ...], list[int]] = {}
    for layer, items in layer_rows.items():
        signature = tuple(
            (item["module_pattern"], item["kernel_name"], item["stream_no"])
            for item in items
        )
        signature_to_layers.setdefault(signature, []).append(layer)

    for signature, layers in signature_to_layers.items():
        layer_count = len(layers)
        label = layer_group_label(layers)
        for idx, (module, kernel, stream_no) in enumerate(signature):
            total = sum(layer_rows[layer][idx]["duration_us"] for layer in layers)
            grouped.append(
                {
                    "layer_group": label,
                    "module": module,
                    "kernel": kernel,
                    "stream_no": stream_no,
                    "time_us": total / layer_count,
                }
            )

    # Keep unmatched in the same output file for follow-up instrumentation work.
    for (kernel, stream_no), total_us in unmatched_accum.items():
        grouped.append(
            {
                "layer_group": "unmatched",
                "module": "<unmatched>",
                "kernel": kernel,
                "stream_no": stream_no,
                "time_us": total_us,
            }
        )

    return grouped


def write_decode_csv(path: str, rows: list[dict[str, Any]], full_decode_us: float) -> None:
    stream_map = remap_streams(
        [
            {"args": {"stream": row["stream"]}}
            for row in rows
        ]
    )
    breakdown_rows = build_grouped_breakdown_rows(rows, stream_map)
    with open(path, "w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(
            [
                "layer_group",
                "module/tag",
                "kernel",
                "stream_id",
                "time_us",
                "percent_of_full_decode_forward",
            ]
        )
        for row in breakdown_rows:
            percent = (
                row["time_us"] / full_decode_us * 100.0 if full_decode_us > 0 else 0.0
            )
            writer.writerow(
                [
                    row["layer_group"],
                    row["module"],
                    row["kernel"],
                    row["stream_no"],
                    f"{row['time_us']:.3f}",
                    f"{percent:.6f}",
                ]
            )


def main() -> None:
    parser = argparse.ArgumentParser(description="Formal ATOM trace parser")
    parser.add_argument("run_trace")
    parser.add_argument("--capture-trace", default=None)
    parser.add_argument(
        "--output",
        default="decode_breakdown.csv",
        help="Output CSV path (default: decode_breakdown.csv).",
    )
    parser.add_argument(
        "--kernel-num",
        type=int,
        default=100,
        help="Number of warmup kernel mappings to print (default: 100).",
    )
    args = parser.parse_args()

    run_events = load_events(args.run_trace)
    capture_trace = args.capture_trace or find_capture_trace(args.run_trace)
    if capture_trace is None:
        raise RuntimeError("Could not auto-discover capture trace; pass --capture-trace.")
    capture_events = load_events(capture_trace)

    decode = find_first_decode(run_events)
    batch_size = decode_batch_size(decode)
    graph = find_capture_graph_for_bs(capture_events, batch_size)
    warmup_start, warmup_end = warmup_window_for_graph(capture_events, graph)
    counts = count_events_in_window(capture_events, warmup_start, warmup_end)

    print(f"Run trace: {args.run_trace}")
    print(f"Capture trace: {capture_trace}")
    print("")
    print("First decode:")
    print(f"  name: {decode.get('name')}")
    print(f"  ts: {decode.get('ts'):.3f}")
    print(f"  dur: {decode.get('dur'):.3f}")
    print(f"  batch size: {batch_size}")
    print("")
    print("Matching capture graph:")
    print(f"  name: {graph.get('name')}")
    print(f"  ts: {graph.get('ts'):.3f}")
    print(f"  dur: {graph.get('dur'):.3f}")
    print("")
    print("Decode warmup window:")
    print(f"  start: {warmup_start:.3f}")
    print(f"  end: {warmup_end:.3f}")
    print(f"  dur: {warmup_end - warmup_start:.3f}")
    print(f"  events: {counts}")
    warmup_mapping = build_warmup_mapping(capture_events, warmup_start, warmup_end)
    print(f"  mapping entries: {len(warmup_mapping)}")
    print_first_warmup_mappings(warmup_mapping, limit=args.kernel_num)

    decode_start, decode_end = decode_gpu_window(run_events, decode)
    replay_kernels = replay_kernels_in_window(run_events, decode_start, decode_end)
    matched_rows = match_replay_to_warmup(replay_kernels, warmup_mapping)
    full_decode_us = decode_end - decode_start
    write_decode_csv(args.output, matched_rows, full_decode_us)
    unmatched = sum(1 for row in matched_rows if row["cpu_module"] == "<unmatched>")
    print("")
    print("Decode replay mapping:")
    print(f"  replay kernels: {len(replay_kernels)}")
    print(f"  unmatched kernels: {unmatched}")
    print(f"  CSV written to: {args.output}")


if __name__ == "__main__":
    main()
