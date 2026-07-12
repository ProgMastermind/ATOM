# SPDX-License-Identifier: MIT
# Regression tests for speculative-config validation in EngineArgs._get_engine_kwargs.

import sys
import ast
from pathlib import Path
from unittest.mock import MagicMock, patch

# conftest.py stubs atom.* and zmq before any atom imports are attempted,
# but arg_utils.py imports LLMEngine from atom and CompilationConfig /
# SpeculativeConfig from atom.config, which the minimal stub doesn't expose.
_atom_stub = sys.modules.get("atom")
if _atom_stub is not None and not hasattr(_atom_stub, "LLMEngine"):
    _atom_stub.LLMEngine = MagicMock()

_atom_config_stub = sys.modules.get("atom.config")
if _atom_config_stub is not None:
    if not hasattr(_atom_config_stub, "CompilationConfig"):
        _atom_config_stub.CompilationConfig = MagicMock(
            side_effect=lambda **kw: MagicMock(**kw)
        )
    if not hasattr(_atom_config_stub, "SpeculativeConfig"):
        _atom_config_stub.SpeculativeConfig = MagicMock(
            side_effect=lambda **kw: MagicMock(**kw)
        )
    if not hasattr(_atom_config_stub, "ParallelConfig"):
        _atom_config_stub.ParallelConfig = MagicMock(
            side_effect=lambda **kw: MagicMock(**kw)
        )

from atom.model_engine.arg_utils import EngineArgs  # noqa: E402


ATOM_ROOT = Path(__file__).resolve().parent.parent


class TestEngineArgsSpeculativeValidation:
    """Regression tests for speculative-config construction in _get_engine_kwargs."""

    def test_no_method_gives_no_speculative_config(self):
        """method=None → speculative_config must be None (no crash)."""
        args = EngineArgs(method=None, num_speculative_tokens=1)
        kwargs = args._get_engine_kwargs()
        assert kwargs.get("speculative_config") is None

    def test_method_mtp_zero_tokens_disables_speculation(self):
        """method='mtp', num_speculative_tokens=0 → treated as disabled,
        speculative_config is None (regression for ZeroDivisionError)."""
        args = EngineArgs(method="mtp", num_speculative_tokens=0)
        kwargs = args._get_engine_kwargs()
        assert kwargs.get("speculative_config") is None

    def test_method_mtp_negative_tokens_disables_speculation(self):
        """method='mtp', num_speculative_tokens=-1 → treated as disabled,
        speculative_config is None."""
        args = EngineArgs(method="mtp", num_speculative_tokens=-1)
        kwargs = args._get_engine_kwargs()
        assert kwargs.get("speculative_config") is None

    def test_method_mtp_valid_tokens_builds_speculative_config(self):
        """method='mtp', num_speculative_tokens=3 → SpeculativeConfig constructed."""
        fake_spec_config = MagicMock()
        with patch(
            "atom.model_engine.arg_utils.SpeculativeConfig",
            return_value=fake_spec_config,
        ) as mock_cls:
            args = EngineArgs(method="mtp", num_speculative_tokens=3)
            kwargs = args._get_engine_kwargs()

        mock_cls.assert_called_once_with(
            method="mtp",
            model=args.model,
            num_speculative_tokens=3,
        )
        assert kwargs["speculative_config"] is fake_spec_config


class TestEngineArgsDistributedDP:
    """Tests for distributed DP/EP engine parameter plumbing."""

    def test_parallel_config_contains_global_and_local_dp_ranks(self):
        args = EngineArgs(
            data_parallel_size=16,
            data_parallel_size_local=8,
            data_parallel_rank=8,
            data_parallel_master_ip="10.0.0.1",
            data_parallel_master_port=29600,
            data_parallel_base_port=29700,
            distributed_dp=True,
            distributed_dp_serving=True,
        )

        kwargs = args._get_engine_kwargs()
        pc = kwargs["parallel_config"]

        assert pc.data_parallel_size == 16
        assert pc.data_parallel_size_local == 8
        assert pc.data_parallel_rank == 8
        assert pc.data_parallel_master_ip == "10.0.0.1"
        assert pc.data_parallel_master_port == 29600
        assert pc.data_parallel_base_port == 29700
        assert pc.distributed_dp is True
        assert kwargs["distributed_dp_serving"] is True
        assert "data_parallel_size" not in kwargs

    def test_moe_backend_selectors_are_engine_parameters(self):
        args = EngineArgs(
            moe_all2all_backend="rccl",
            rccl_moe_impl="triton_batched_gemm",
            mori_all2all_mode="low-latency",
        )

        kwargs = args._get_engine_kwargs()

        assert kwargs["moe_all2all_backend"] == "rccl"
        assert kwargs["rccl_moe_impl"] == "triton_batched_gemm"
        assert kwargs["mori_all2all_mode"] == "low-latency"
        assert kwargs["enable_low_latency"] is True


class TestModelRunnerDistributedInit:
    """Regression tests for distributed init argument plumbing."""

    def test_model_runner_passes_local_device_rank_to_aiter_init(self):
        source = (ATOM_ROOT / "atom/model_engine/model_runner.py").read_text()
        tree = ast.parse(source)

        matching_calls = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and getattr(node.func, "id", None) == "init_dist_env"
            and any(
                kw.arg == "local_rank"
                and isinstance(kw.value, ast.Name)
                and kw.value.id == "local_device_rank"
                for kw in node.keywords
            )
        ]

        assert matching_calls, "ModelRunner must pass local_device_rank as local_rank"


class TestMoEAll2AllBackendRouting:
    """Regression tests for backend-specific MoE all2all setup."""

    def test_rccl_prepare_finalize_does_not_touch_mori_all2all_manager_first(self):
        source = (ATOM_ROOT / "atom/model_ops/moe.py").read_text()
        tree = ast.parse(source)

        target = None
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "_maybe_make_prepare_finalize":
                target = node
                break
        assert target is not None

        rccl_check_line = None
        for node in ast.walk(target):
            if (
                isinstance(node, ast.Compare)
                and isinstance(node.left, ast.Attribute)
                and node.left.attr == "ATOM_ALL2ALL_BACKEND"
                and any(isinstance(comp, ast.Constant) and comp.value == "rccl" for comp in node.comparators)
            ):
                rccl_check_line = node.lineno
                break
        assert rccl_check_line is not None

        early_all2all_manager_accesses = [
            node.lineno
            for node in ast.walk(target)
            if isinstance(node, ast.Attribute)
            and node.attr == "all2all_manager"
            and node.lineno < rccl_check_line
        ]
        assert not early_all2all_manager_accesses

    def test_rccl_batched_moe_disables_triton_decode_fast_path(self):
        source = (ATOM_ROOT / "atom/model_ops/moe.py").read_text()
        tree = ast.parse(source)

        guarded_disable = False
        for node in ast.walk(tree):
            if not isinstance(node, ast.If):
                continue
            test_src = ast.get_source_segment(source, node.test) or ""
            if "ATOM_ALL2ALL_BACKEND" not in test_src or "rccl" not in test_src:
                continue
            if not all(
                impl in test_src
                for impl in ("batched", "flydsl_batched_gemm", "triton_batched_gemm")
            ):
                continue
            guarded_disable = any(
                isinstance(child, ast.Assign)
                and any(
                    isinstance(target, ast.Attribute)
                    and target.attr == "use_triton_decode"
                    for target in child.targets
                )
                and isinstance(child.value, ast.Constant)
                and child.value.value is False
                for child in ast.walk(node)
            )
            if guarded_disable:
                break

        assert guarded_disable
