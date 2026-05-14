import functools
from dataclasses import replace

import torch
from atom.plugin.vllm.platform import disable_vllm_plugin_attention

try:
    from vllm.v1.attention.backends.mla.prefill.base import MLAPrefillBackend
except Exception:  # pragma: no cover - imported only when vLLM is available
    MLAPrefillBackend = object


class ATOMPluginMLAPrefillBackend(MLAPrefillBackend):
    """Placeholder backend for ATOM MLA plugin mode.

    vLLM 0.21.0 introduced a mandatory MLA prefill backend selector during
    `MLAAttention` initialization. ATOM's plugin-mode MLA path already owns
    prefill execution inside `forward_impl_plugin_mode`, so the upstream
    prefill backend must not be selected for ATOM's custom MLA backend.
    """

    @staticmethod
    def get_name() -> str:
        return "ATOM_PLUGIN_MLA_PREFILL"

    def prepare_metadata(self, prefill_metadata) -> None:
        self._prefill_metadata = prefill_metadata

    def run_prefill_new_tokens(
        self,
        q: torch.Tensor,
        k: torch.Tensor,
        v: torch.Tensor,
        return_softmax_lse: bool,
    ) -> torch.Tensor | tuple[torch.Tensor, torch.Tensor]:
        raise RuntimeError(
            "ATOM MLA plugin mode handles prefill inside "
            "forward_impl_plugin_mode; vLLM's standalone MLA prefill backend "
            "must not be called."
        )

    def run_prefill_context_chunk(
        self,
        chunk_idx: int,
        q: torch.Tensor,
        k: torch.Tensor,
        v: torch.Tensor,
    ) -> tuple[torch.Tensor, torch.Tensor]:
        raise RuntimeError(
            "ATOM MLA plugin mode handles chunked prefill inside "
            "forward_impl_plugin_mode; vLLM's standalone MLA prefill backend "
            "must not be called."
        )


def select_mla_prefill_backend_for_plugin(vllm_config, default_selector):
    """Bridge vLLM's MLA prefill selector to ATOM's plugin-owned prefill path."""

    if disable_vllm_plugin_attention:
        return default_selector(vllm_config)

    compilation_config = getattr(vllm_config, "compilation_config", None)
    static_forward_context = getattr(compilation_config, "static_forward_context", None)
    if static_forward_context:
        current_layer = next(reversed(static_forward_context.values()))
        attn_backend = getattr(current_layer, "attn_backend", None)
        backend_name = getattr(attn_backend, "get_name", lambda: None)()
        if isinstance(backend_name, str) and backend_name.startswith("CUSTOM"):
            return ATOMPluginMLAPrefillBackend

    return default_selector(vllm_config)


def set_default_quant_scales(
    layer: torch.nn.Module, register_buffer: bool = False
) -> None:
    """Sets default quantization scales for the layer."""
    if register_buffer:
        layer.register_buffer("_k_scale", torch.tensor(1.0, dtype=torch.float32))
        layer.register_buffer("_v_scale", torch.tensor(1.0, dtype=torch.float32))
        layer.register_buffer("_q_scale", torch.tensor(1.0, dtype=torch.float32))
        layer.register_buffer("_prob_scale", torch.tensor(1.0, dtype=torch.float32))
    else:
        layer._k_scale.fill_(1.0)
        layer._v_scale.fill_(1.0)
        layer._q_scale.fill_(1.0)
        layer._prob_scale.fill_(1.0)

    # We also keep q/k/v_scale on host (cpu) memory for attention
    # backends that require the scales to be on host instead of on device.
    # e.g. Flashinfer
    layer._q_scale_float = 1.0
    layer._k_scale_float = 1.0
    layer._v_scale_float = 1.0
    layer._prob_scale_float = 1.0


def _patch_vllm_mla_attention_process_weights_after_loading(mla_attention_cls) -> None:
    """
    We patch the process_weights_after_loading method one reason is that
    orig_process_weights_after_loading need a act_dtype parameter,
    but in atom, we don't have this parameter. if disable_vllm_plugin_attention,
    we will fallback to original vllm attention backend.
    """
    orig_process_weights_after_loading = mla_attention_cls.process_weights_after_loading
    if getattr(
        orig_process_weights_after_loading,
        "_atom_mla_process_weights_after_loading_patched",
        False,
    ):
        return

    @functools.wraps(orig_process_weights_after_loading)
    def _process_weights_after_loading(self, act_dtype: torch.dtype = torch.bfloat16):
        if disable_vllm_plugin_attention:
            return orig_process_weights_after_loading(self, act_dtype)

        if hasattr(self.impl, "process_weights_after_loading"):
            self.impl.process_weights_after_loading()

        set_default_quant_scales(self, register_buffer=False)

    setattr(
        _process_weights_after_loading,
        "_atom_mla_process_weights_after_loading_patched",
        True,
    )
    mla_attention_cls.process_weights_after_loading = _process_weights_after_loading


def _patch_vllm_mla_prefill_backend_selector(mla_attention_cls) -> None:
    module_globals = mla_attention_cls.__init__.__globals__
    orig_selector = module_globals.get("get_mla_prefill_backend")
    if orig_selector is None:
        return

    if getattr(orig_selector, "_atom_mla_prefill_selector_patched", False):
        return

    @functools.wraps(orig_selector)
    def _selector(vllm_config):
        return select_mla_prefill_backend_for_plugin(vllm_config, orig_selector)

    setattr(_selector, "_atom_mla_prefill_selector_patched", True)
    module_globals["get_mla_prefill_backend"] = _selector


def _patch_vllm_mla_attention_forward_impl(mla_attention_cls) -> None:
    """
    We patch the forward_impl method is to make qk rope and kv cache update
    can be fused in attention forward pass.
    """
    orig_forward_impl = mla_attention_cls.forward_impl
    if getattr(orig_forward_impl, "_atom_mla_forward_impl_patched", False):
        return

    @functools.wraps(orig_forward_impl)
    def _forward_impl(
        self,
        q: torch.Tensor,
        k_c_normed: torch.Tensor,
        k_pe: torch.Tensor,
        kv_cache: torch.Tensor,
        attn_metadata,
        output: torch.Tensor | None = None,
        output_scale: torch.Tensor | None = None,
        output_block_scale: torch.Tensor | None = None,
        **kwargs,
    ) -> torch.Tensor:
        if disable_vllm_plugin_attention:
            return orig_forward_impl(
                self,
                q,
                k_c_normed,
                k_pe,
                kv_cache,
                attn_metadata,
                output=output,
                output_scale=output_scale,
                output_block_scale=output_block_scale,
                **kwargs,
            )

        if hasattr(self.impl, "forward_impl_plugin_mode"):
            return self.impl.forward_impl_plugin_mode(
                self,
                q,
                k_c_normed,
                k_pe,
                kv_cache,
                attn_metadata=attn_metadata,
                output=output,
            )

        return orig_forward_impl(
            self,
            q,
            k_c_normed,
            k_pe,
            kv_cache,
            attn_metadata,
            output=output,
            output_scale=output_scale,
            output_block_scale=output_block_scale,
            **kwargs,
        )

    setattr(_forward_impl, "_atom_mla_forward_impl_patched", True)
    mla_attention_cls.forward_impl = _forward_impl


def _patch_vllm_mla_attention_get_kv_cache_spec(mla_attention_cls) -> None:
    """
    vLLM's MLAAttention may set the kv cache dtype to fp8_ds_mla, which uses
    656 bytes per block and is not compatible with ATOM's standard 576-per-block
    fp8 layout. Therefore, we patch it so that in vLLM plugin mode, the layout
    of the kv cache is not in the fp8_ds_mla format.
    """

    orig_get_kv_cache_spec = mla_attention_cls.get_kv_cache_spec
    if getattr(orig_get_kv_cache_spec, "_atom_mla_get_kv_cache_spec_patched", False):
        return

    @functools.wraps(orig_get_kv_cache_spec)
    def _patched_get_kv_cache_spec(self, vllm_config):
        if disable_vllm_plugin_attention:
            return orig_get_kv_cache_spec(self, vllm_config)

        spec = orig_get_kv_cache_spec(self, vllm_config)

        if (
            hasattr(spec, "cache_dtype_str")
            and spec.cache_dtype_str == "fp8_ds_mla"
            and getattr(self, "use_sparse", False)
        ):
            spec = replace(spec, cache_dtype_str=None)

        return spec

    mla_attention_cls.get_kv_cache_spec = _patched_get_kv_cache_spec
    setattr(
        _patched_get_kv_cache_spec,
        "_atom_mla_get_kv_cache_spec_patched",
        True,
    )


def patch_vllm_mla_attention() -> None:
    try:
        from vllm.attention.layer import MLAAttention
    except ImportError:
        from vllm.model_executor.layers.attention import MLAAttention

    _patch_vllm_mla_prefill_backend_selector(MLAAttention)
    _patch_vllm_mla_attention_get_kv_cache_spec(MLAAttention)
    _patch_vllm_mla_attention_process_weights_after_loading(MLAAttention)
    _patch_vllm_mla_attention_forward_impl(MLAAttention)
