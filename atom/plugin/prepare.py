from typing import Any
import logging

logger = logging.getLogger("atom")

# all of the supported frameworks, including server mode and plugin mode
_SUPPORTED_FRAMEWORKS = ["vllm", "sglang", "sgl", "atom", "rtpllm"]

# supported frameworks for plugin mode
_SUPPORTED_FRAMEWORKS_FOR_PLUGIN_MODE = ["vllm", "sglang", "sgl", "rtpllm"]

# default is atom for server mode
_CURRENT_FRAMEWORK = "atom"


def is_sglang() -> bool:
    global _CURRENT_FRAMEWORK
    return bool(_CURRENT_FRAMEWORK.lower() in ["sglang", "sgl"])


def is_vllm() -> bool:
    global _CURRENT_FRAMEWORK
    return bool(_CURRENT_FRAMEWORK.lower() in ["vllm"])


def is_rtpllm() -> bool:
    global _CURRENT_FRAMEWORK
    return bool(_CURRENT_FRAMEWORK.lower() in ["rtpllm"])


def is_plugin_mode() -> bool:
    global _CURRENT_FRAMEWORK
    return bool(_CURRENT_FRAMEWORK.lower() in _SUPPORTED_FRAMEWORKS_FOR_PLUGIN_MODE)


def _set_framework_backbone(framework: str) -> None:
    if framework.lower() not in _SUPPORTED_FRAMEWORKS:
        raise ValueError(f"Unsupported framework {framework} for ATOM to plug in")
    global _CURRENT_FRAMEWORK
    _CURRENT_FRAMEWORK = framework


def prepare_model(config: Any, engine: str):
    """
    Prepare ATOM model for plugin mode upper frameworks.
    """
    logger.info(f"Prepare model for plugin mode, the upper engine is {engine}")

    _set_framework_backbone(engine)

    if not (is_sglang() or is_rtpllm()):
        raise ValueError(
            f"prepare_model does not support engine {engine!r} "
            f"with config type {type(config)}"
        )

    # import here to avoid partial initialization
    from .register import (
        _ATOM_SUPPORTED_MODELS,
        # register_ops_to_vllm,
        register_ops_to_sglang,
        init_aiter_dist,
        set_attn_cls,
    )

    from atom.plugin.config import generate_atom_config_for_plugin_mode

    atom_config = generate_atom_config_for_plugin_mode(config)

    if not hasattr(atom_config.hf_config, "architectures"):
        raise ValueError("Failed to parse model architectures from HF config")
    model_arch = atom_config.hf_config.architectures[0]

    if model_arch not in _ATOM_SUPPORTED_MODELS:
        supported_archs = list(_ATOM_SUPPORTED_MODELS.keys())
        raise ValueError(
            f"ATOM does not support the required model architecture: {model_arch}. "
            f"For now supported model architectures: {supported_archs}"
        )

    model_cls = _ATOM_SUPPORTED_MODELS[model_arch]
    logger.info(f"ATOM model class for {model_arch} is {model_cls}")

    # rtp-llm plugin mode uses atom.prepare_model -> model construction directly.
    # Ensure quant layer name remap/exclude processing is done BEFORE model init,
    # otherwise layer quant_type gets fixed with stale rules.
    if is_rtpllm():
        conv1d_exclude = "model.layers.*.linear_attn.conv1d"
        if conv1d_exclude not in atom_config.quant_config.exclude_layers:
            atom_config.quant_config.exclude_layers.append(conv1d_exclude)
            logger.info(
                "rtp-llm plugin: add quant exclude for incompatible layer pattern: %s",
                conv1d_exclude,
            )

        atom_config.quant_config.remap_layer_name(
            atom_config.hf_config,
            packed_modules_mapping=getattr(model_cls, "packed_modules_mapping", {}),
            quant_exclude_name_mapping=getattr(
                model_cls, "quant_exclude_name_mapping", {}
            ),
        )

    if is_sglang():
        register_ops_to_sglang(atom_config=atom_config)

    set_attn_cls()

    # init aiter dist for using aiter custom collective ops
    init_aiter_dist(config=atom_config)

    return model_cls(atom_config=atom_config)
