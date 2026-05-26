from __future__ import annotations

import os
import logging
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Iterable, Optional

import torch

from atom.plugin.sglang.models.base_model_wrapper import SGLangForwardBatchMetadata
from atom.utils.forward_context import AttentionMetaData, Context, ForwardContext

logger = logging.getLogger("atom.plugin.sglang.tbo")


def enabled() -> bool:
    return os.environ.get("ATOM_SGLANG_FINE_GRAIN_TBO", "1").lower() not in {
        "0",
        "false",
        "no",
        "off",
    }


def debug_enabled() -> bool:
    return os.environ.get("ATOM_SGLANG_TBO_DEBUG", "0").lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def can_run(forward_batch: Any) -> bool:
    children = getattr(forward_batch, "tbo_children", None)
    if getattr(forward_batch, "forward_mode", None) is None:
        return False
    return bool(
        enabled()
        and getattr(forward_batch, "can_run_tbo", False)
        and children is not None
        and len(children) == 2
        and all(child is not None for child in children)
    )


def run_deepseek_tbo(
    *,
    layers: Iterable[Any],
    positions: torch.Tensor,
    hidden_states: torch.Tensor,
    residual: Optional[torch.Tensor],
    forward_batch: Any,
) -> tuple[torch.Tensor, Optional[torch.Tensor]]:
    layers = list(layers)
    if not layers:
        return hidden_states, residual
    for layer in layers:
        mlp = getattr(layer, "mlp", None)
        experts = getattr(mlp, "experts", None)
        if not getattr(experts, "supports_fine_grain_tbo", lambda: False)():
            raise RuntimeError(
                f"Layer {type(layer).__name__} does not support fine-grain MoE TBO"
            )

    children = list(forward_batch.tbo_children)
    if debug_enabled():
        logger.info(
            "[SGL+ATOM fine-grain TBO] enter parent=%s global=%s "
            "tokens=%s children=%s",
            _mode_name(getattr(forward_batch, "forward_mode", None)),
            _mode_name(getattr(forward_batch, "global_forward_mode", None)),
            int(positions.shape[0]),
            [_child_debug(child) for child in children],
        )
    child_inputs = [
        _filter_child_inputs(
            child=child,
            positions=positions,
            hidden_states=hidden_states,
            residual=residual,
            subbatch_index=index,
        )
        for index, child in enumerate(children)
    ]
    original_len = int(hidden_states.shape[0])

    operations = _build_deepseek_operations(layers, forward_batch.global_forward_mode)
    stages = _convert_operations_to_stages(operations)
    executors = [
        _StageExecutor(f"child{index}", stages, inputs)
        for index, inputs in enumerate(child_inputs)
    ]
    delta = _tbo_delta_stages(forward_batch.global_forward_mode)

    for _ in range(delta):
        _next_child(executors[0], children[0])

    for _ in range(executors[0].num_stages - delta):
        _next_child(executors[0], children[0])
        _next_child(executors[1], children[1])

    for _ in range(delta):
        _next_child(executors[1], children[1])

    assert executors[0].done and executors[1].done
    return _merge_outputs(
        executors[0].output,
        executors[1].output,
        children,
        original_len,
    )


def _build_deepseek_operations(layers: list[Any], forward_mode: Any) -> list[Any]:
    operations: list[Any] = []
    for layer in layers:
        if not hasattr(layer, "op_comm_prepare_attn") or not hasattr(
            getattr(layer, "mlp", None), "op_gate"
        ):
            raise AssertionError(
                f"Layer {type(layer).__name__} does not expose fine-grain TBO ops"
            )
        if forward_mode.is_decode():
            operations.extend(_decode_layer_operations(layer))
        else:
            operations.extend(_prefill_layer_operations(layer))
    return operations


def _prefill_layer_operations(layer: Any) -> list[Any]:
    return [
        layer.op_comm_prepare_attn,
        layer.self_attn.op_prepare,
        layer.self_attn.op_core,
        layer.op_comm_prepare_mlp,
        layer.mlp.op_gate,
        layer.mlp.op_select_experts,
        layer.mlp.op_dispatch_a,
        YieldOperation(),
        layer.mlp.op_dispatch_b,
        layer.mlp.op_experts,
        layer.mlp.op_combine_a,
        YieldOperation(),
        layer.mlp.op_shared_experts,
        layer.mlp.op_combine_b,
        layer.mlp.op_output,
        layer.op_comm_postprocess_layer,
    ]


def _decode_layer_operations(layer: Any) -> list[Any]:
    return [
        layer.op_comm_prepare_attn,
        layer.self_attn.op_prepare,
        YieldOperation(),
        layer.self_attn.op_core,
        layer.op_comm_prepare_mlp,
        layer.mlp.op_gate,
        layer.mlp.op_select_experts,
        YieldOperation(),
        layer.mlp.op_dispatch_a,
        layer.mlp.op_shared_experts,
        YieldOperation(),
        layer.mlp.op_dispatch_b,
        layer.mlp.op_experts,
        layer.mlp.op_combine_a,
        YieldOperation(),
        layer.mlp.op_combine_b,
        YieldOperation(),
        layer.mlp.op_output,
        layer.op_comm_postprocess_layer,
    ]


def _tbo_delta_stages(forward_mode: Any) -> int:
    return 2 if forward_mode.is_decode() else 0


def _filter_child_inputs(
    *,
    child: Any,
    positions: torch.Tensor,
    hidden_states: torch.Tensor,
    residual: Optional[torch.Tensor],
    subbatch_index: int,
) -> dict[str, Any]:
    start, end = child.tbo_parent_token_range
    padded_len = int(child.positions.shape[0])
    return {
        "positions": child.positions,
        "hidden_states": _pad_to_len(hidden_states[start:end], padded_len),
        "residual": None
        if residual is None
        else _pad_to_len(residual[start:end], padded_len),
        "forward_batch": child,
        "tbo_subbatch_index": subbatch_index,
    }


def _pad_to_len(tensor: torch.Tensor, padded_len: int) -> torch.Tensor:
    if int(tensor.shape[0]) == padded_len:
        return tensor
    output = tensor.new_zeros((padded_len, *tensor.shape[1:]))
    output[: tensor.shape[0]] = tensor
    return output


def _merge_outputs(output_a, output_b, children: list[Any], original_len: int):
    hidden_a = output_a["hidden_states"]
    hidden_b = output_b["hidden_states"]
    residual_a = output_a["residual"]
    residual_b = output_b["residual"]
    merged_hidden = hidden_a.new_zeros((original_len, *hidden_a.shape[1:]))
    merged_residual = (
        None
        if residual_a is None
        else residual_a.new_zeros((original_len, *residual_a.shape[1:]))
    )

    for output, hidden, residual, child in (
        (output_a, hidden_a, residual_a, children[0]),
        (output_b, hidden_b, residual_b, children[1]),
    ):
        del output
        start, end = child.tbo_parent_token_range
        real_len = end - start
        merged_hidden[start:end] = hidden[:real_len]
        if merged_residual is not None:
            assert residual is not None
            merged_residual[start:end] = residual[:real_len]
    return merged_hidden, merged_residual


def _next_child(executor: "_StageExecutor", child: Any) -> None:
    with _child_context(child):
        _set_child_dp_buffer_len(child)
        executor.next()


@contextmanager
def _child_context(child: Any):
    from atom.utils.forward_context import _forward_context_local, get_forward_context

    parent_ctx = get_forward_context()
    previous_ctx = getattr(_forward_context_local, "ctx", None)
    _forward_context_local.ctx = _make_child_atom_context(parent_ctx, child)
    child_metadata = SGLangForwardBatchMetadata.build(child)
    try:
        with SGLangForwardBatchMetadata.bind(child_metadata):
            yield
    finally:
        if previous_ctx is None:
            try:
                del _forward_context_local.ctx
            except AttributeError:
                pass
        else:
            _forward_context_local.ctx = previous_ctx


def _make_child_atom_context(parent_ctx: ForwardContext, child: Any) -> ForwardContext:
    forward_mode = child.forward_mode
    global_forward_mode = getattr(child, "global_forward_mode", None)
    context_forward_mode = global_forward_mode or forward_mode
    is_prefill = context_forward_mode.is_prefill()
    batch_size = int(child.batch_size)
    num_tokens = int(child.positions.shape[0])
    return ForwardContext(
        attn_metadata=AttentionMetaData(
            max_seqlen_q=1 if context_forward_mode.is_decode_or_idle() else 0
        ),
        no_compile_layers=parent_ctx.no_compile_layers,
        kv_cache_data=parent_ctx.kv_cache_data,
        context=Context(
            positions=child.positions,
            is_prefill=is_prefill,
            is_dummy_run=getattr(forward_mode, "is_idle", lambda: False)(),
            batch_size=batch_size,
            graph_bs=num_tokens if is_prefill else batch_size,
        ),
        dp_metadata=parent_ctx.dp_metadata,
        spec_decode_metadata=None,
        ubatch_slices=None,
        main_stream=parent_ctx.main_stream,
        in_hipgraph=parent_ctx.in_hipgraph,
    )


def _set_child_dp_buffer_len(child: Any) -> None:
    global_dp_buffer_len = getattr(child, "global_dp_buffer_len", None)
    if global_dp_buffer_len is None:
        return
    from sglang.srt.layers.dp_attention import set_dp_buffer_len

    padding_mode = getattr(child, "dp_padding_mode", None)
    set_dp_buffer_len(
        global_dp_buffer_len,
        getattr(child, "tbo_padded_len", None),
        False if padding_mode is None else padding_mode.is_max_len(),
        getattr(child, "global_num_tokens_cpu", None),
    )


class YieldOperation:
    pass


@dataclass
class ExecutionOperation:
    debug_name: str
    fn: Any


class _StageExecutor:
    def __init__(self, name: str, stages: list[list[ExecutionOperation]], inputs):
        self.name = name
        self.stages = stages
        self.index = 0
        self.state = _StateDict()
        self.stage_output = inputs

    def next(self) -> None:
        assert not self.done
        for op in self.stages[self.index]:
            self.stage_output = op.fn(
                state=self.state,
                **(self.stage_output if self.stage_output is not None else {}),
            )
        self.index += 1

    @property
    def done(self) -> bool:
        return self.index >= self.num_stages

    @property
    def num_stages(self) -> int:
        return len(self.stages)

    @property
    def output(self):
        assert self.done
        return self.stage_output


class _StateDict:
    def __init__(self):
        self._data: dict[str, Any] = {}

    def __setattr__(self, key: str, value: Any) -> None:
        if key == "_data":
            super().__setattr__(key, value)
            return
        if key in self._data:
            raise AssertionError(f"`{key}` already exists")
        self._data[key] = value

    def __getattr__(self, key: str) -> Any:
        return self._data[key]

    def pop(self, key: str) -> Any:
        return self._data.pop(key)

    def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, default)

    def update(self, values: dict[str, Any]) -> None:
        for key, value in values.items():
            setattr(self, key, value)

    def clear(self, expect_keys: set[str]) -> None:
        actual = set(self._data)
        if actual != expect_keys:
            raise AssertionError(
                f"Unexpected state keys: actual={sorted(actual)}, "
                f"expected={sorted(expect_keys)}"
            )
        self._data.clear()


def _convert_operations_to_stages(operations: Iterable[Any]):
    stages = []
    pending = []
    for op in operations:
        if isinstance(op, YieldOperation):
            if not pending:
                raise AssertionError("YieldOperation cannot create empty stage")
            stages.append(pending)
            pending = []
        else:
            pending.append(
                ExecutionOperation(
                    debug_name=getattr(op, "__name__", "unknown"),
                    fn=op,
                )
            )
    if pending:
        stages.append(pending)
    return stages


def _mode_name(mode: Any) -> str:
    if mode is None:
        return "None"
    name = getattr(mode, "name", None)
    return str(name if name is not None else mode)


def _child_debug(child: Any) -> dict[str, Any]:
    if child is None:
        return {"child": None}
    return {
        "mode": _mode_name(getattr(child, "forward_mode", None)),
        "global": _mode_name(getattr(child, "global_forward_mode", None)),
        "bs": int(getattr(child, "batch_size", 0) or 0),
        "tokens": int(getattr(child, "positions", ()).shape[0])
        if getattr(child, "positions", None) is not None
        else None,
        "range": getattr(child, "tbo_parent_token_range", None),
        "padded": getattr(child, "tbo_padded_len", None),
        "global_tokens": getattr(child, "global_num_tokens_cpu", None),
    }
