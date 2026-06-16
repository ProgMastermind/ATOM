# SPDX-License-Identifier: MIT
# Copyright (C) 2024-2025, Advanced Micro Devices, Inc. All rights reserved.

"""MiniMax-M3 expert weight mapping helpers."""


def make_minimax_m3_expert_params_mapping(
    num_experts: int,
) -> list[tuple[str, str, int, str]]:
    """Return loader mapping for MiniMax-M3 split expert checkpoint weights."""
    mapping: list[tuple[str, str, int, str]] = []
    for expert_id in range(num_experts):
        for shard_id, weight_names in (
            ("w1", ("w1", "gate_proj")),
            ("w2", ("w2", "down_proj")),
            ("w3", ("w3", "up_proj")),
        ):
            if shard_id in ("w1", "w3"):
                param_prefix = "experts.w13_"
                scale_param = "experts.w13_weight_scale"
            else:
                param_prefix = "experts.w2_"
                scale_param = "experts.w2_weight_scale"
            for weight_name in weight_names:
                mapping.append(
                    (
                        scale_param,
                        f"experts.{expert_id}.{weight_name}.scale",
                        expert_id,
                        shard_id,
                    )
                )
                mapping.append(
                    (
                        param_prefix,
                        f"experts.{expert_id}.{weight_name}.",
                        expert_id,
                        shard_id,
                    )
                )
    return mapping
