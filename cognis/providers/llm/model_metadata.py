"""Provider-scoped metadata resolution, separate from inference transport routing."""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import litellm

from cognis import json_codec as json
from cognis.models.config import ModelInfo
from cognis.providers.llm.hosted import (
    is_systemone_model,
    model_mode_from_metadata,
    systemone_metadata,
)
from cognis.providers.llm.reasoning import reasoning_efforts_for_model

# Exact hosts only: an arbitrary compatible endpoint must not inherit OpenAI prices.
CATALOG_HOSTS = {
    "api.openai.com": "openai",
    "api.groq.com": "groq",
    "api.cerebras.ai": "cerebras",
    "api.deepseek.com": "deepseek",
    "api.mistral.ai": "mistral",
    "api.together.xyz": "together_ai",
    "api.together.ai": "together_ai",
    "api.fireworks.ai": "fireworks_ai",
    "openrouter.ai": "openrouter",
    "generativelanguage.googleapis.com": "gemini",
    "api.x.ai": "xai",
    "api.perplexity.ai": "perplexity",
}

ALIASES = {
    "supports_tools": ("supports_function_calling",),
    "supports_parallel_tools": ("supports_parallel_function_calling",),
    "supports_structured_output": ("supports_response_schema",),
    "supports_streaming": ("supports_native_streaming",),
    "context_window": ("max_context_window_tokens", "context_length", "max_context_window"),
    "max_output_tokens": ("max_completion_tokens",),
    "model_mode": ("mode",),
    "supports_tool_search": ("supports_builtin_tool_search",),
    "supports_verbosity": ("support_verbosity",),
    "supported_openai_params": ("supported_parameters",),
    "reasoning_efforts": ("supported_reasoning_efforts",),
}
TOKEN_PRICES = {
    "input_cost_per_mtok": "input_cost_per_token",
    "output_cost_per_mtok": "output_cost_per_token",
    "cache_read_cost_per_mtok": "cache_read_input_token_cost",
    "cache_write_cost_per_mtok": "cache_creation_input_token_cost",
}
LIMIT_FIELDS = {
    "context_window",
    "max_context_window",
    "max_input_tokens",
    "max_output_tokens",
    "max_tools",
}


BUNDLED_CATALOG = Path(__file__).parent / "data" / "litellm_model_prices.json"
_bundled_catalog: dict[str, Any] | None = None


def bundled_catalog() -> dict[str, Any]:
    """Load the reviewed LiteLLM snapshot once and overlay it on the package map.

    The pinned LiteLLM package map lags model releases. The snapshot is refreshed
    by ``scripts/update_model_prices.py``, with reviewed per-model corrections
    from provider documentation when needed. It is never fetched at runtime.
    """
    global _bundled_catalog
    if _bundled_catalog is None:
        snapshot = json.loads(BUNDLED_CATALOG.read_bytes())
        models = snapshot.get("models", {}) if isinstance(snapshot, dict) else {}
        for name, entry in models.items():
            if isinstance(name, str) and isinstance(entry, dict):
                litellm.model_cost[name] = dict(entry)
        _bundled_catalog = {key: snapshot[key] for key in ("source", "fetched_at", "sha256")}
    return _bundled_catalog


bundled_catalog()


def catalog_provider_for(preset: str, base_url: str, explicit: str | None = None) -> str | None:
    """Resolve hosting identity without treating a model namespace as a provider."""
    if explicit and explicit.strip():
        return explicit.strip().lower()
    host = urlsplit(base_url).hostname
    if host in CATALOG_HOSTS:
        return CATALOG_HOSTS[host]
    if preset in {"openai_compatible", "custom"}:
        return None
    return preset.lower() or None


def _number(value: Any, *, positive: bool = False) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float | str):
        return None
    try:
        number = float(value)
    except ValueError:
        return None
    return number if math.isfinite(number) and (number > 0 if positive else number >= 0) else None


def normalize_model_metadata(info: dict[str, Any]) -> dict[str, Any]:
    """Normalize supplied fields only; absent/null capabilities remain unknown."""
    info = dict(info)
    if isinstance(info.get("model_info"), dict):
        info.update(info["model_info"])
    # OpenRouter reports per-token prices in its pricing object.
    if isinstance(info.get("pricing"), dict):
        for remote_key, key in {
            "prompt": "input_cost_per_token",
            "completion": "output_cost_per_token",
        }.items():
            if remote_key in info["pricing"]:
                info.setdefault(key, info["pricing"][remote_key])
    result: dict[str, Any] = {}
    for field in ModelInfo.model_fields:
        if field in {
            "model_id",
            "provider_metadata",
            "runtime_metadata",
            "pricing",
            "untiered_tariff",
        }:
            continue
        value = info.get(field)
        if value is None:
            value = next(
                (info[key] for key in ALIASES.get(field, ()) if info.get(key) is not None), None
            )
        if value is None:
            continue
        if field in LIMIT_FIELDS:
            number = _number(value, positive=True)
            if number is not None and number.is_integer():
                result[field] = int(number)
        elif field.startswith("supports_"):
            if isinstance(value, bool):
                result[field] = value
        elif field.endswith("_per_mtok"):
            number = _number(value)
            if number is not None:
                result[field] = number
        elif isinstance(value, str):
            result[field] = value
        elif isinstance(value, list) and all(isinstance(item, str) for item in value):
            result[field] = list(value)
    if "context_window" not in result and "max_input_tokens" in result:
        result["context_window"] = result["max_input_tokens"]
    if "context_window" in result:
        result.setdefault("max_context_window", result["context_window"])
    for field, alias in TOKEN_PRICES.items():
        number = _number(info.get(alias))
        if field not in result and number is not None:
            result[field] = round(number * 1_000_000, 8)
    pricing = {
        key: value
        for key, value in info.items()
        if ("cost_per_" in key or "_token_cost" in key) and _number(value) is not None
    }
    if isinstance(info.get("pricing"), dict):
        pricing.update(
            {
                key: value
                for key, value in info["pricing"].items()
                if isinstance(key, str) and _number(value) is not None
            }
        )
    if isinstance(info.get("tiered_pricing"), dict | list):
        pricing["tiered_pricing"] = info["tiered_pricing"]
    if pricing:
        result["pricing"] = pricing
    mode = model_mode_from_metadata(info)
    if mode is not None:
        result["model_mode"] = mode
        result["supports_embedding"] = mode == "embedding"
        if mode == "systemone":
            result.update(systemone_metadata())
        elif mode == "image_generation":
            result.update(
                supports_image_generation=True,
                supports_tools=False,
                supports_parallel_tools=False,
                supports_streaming=False,
                supports_structured_output=False,
                supports_reasoning=False,
            )
        elif mode == "video":
            result.update(
                supports_video_generation=True,
                supports_tools=False,
                supports_parallel_tools=False,
                supports_streaming=False,
                supports_structured_output=False,
            )
    architecture = info.get("architecture")
    if isinstance(architecture, dict):
        outputs = architecture.get("output_modalities")
        if isinstance(outputs, list) and "image" in outputs:
            result["supports_image_generation"] = True
    params = result.get("supported_openai_params")
    if isinstance(params, list):
        result.setdefault("supports_tools", "tools" in params)
        result.setdefault("supports_structured_output", "response_format" in params)
        result.setdefault(
            "supports_reasoning", "reasoning" in params or "reasoning_effort" in params
        )
    return result


def resolve_model_metadata(
    model_id: str,
    *,
    preset: str,
    base_url: str = "",
    catalog_provider: str | None = None,
    remote: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Combine hosting-provider catalog and remote metadata with field provenance."""
    hosting = catalog_provider_for(preset, base_url, catalog_provider)
    if is_systemone_model(model_id, preset, remote):
        remote = {**(remote or {}), **systemone_metadata()}
    catalog: dict[str, Any] = {}
    status = "unavailable"
    if hosting:
        # Only strip our hosting prefix; publisher namespaces belong to the wire ID.
        lookup = model_id if model_id.startswith(f"{hosting}/") else f"{hosting}/{model_id}"
        try:
            entry = litellm.model_cost.get(lookup) or litellm.model_cost.get(
                model_id.removeprefix(f"{hosting}/")
            )
            # LiteLLM's chat-oriented projection drops image pricing fields and
            # fabricates chat parameters. Native image metadata uses the same
            # provider-scoped catalog entry without that lossy projection.
            info: Any = (
                entry
                if isinstance(entry, dict)
                and entry.get("litellm_provider") == hosting
                and entry.get("mode") == "image_generation"
                else litellm.get_model_info(model=lookup, custom_llm_provider=hosting)
            )
            if isinstance(info, dict) and info.get("litellm_provider") in {None, hosting}:
                catalog = normalize_model_metadata(info)
                status = "matched"
                params = catalog.get("supported_openai_params")
                if isinstance(params, list) and "stream" in params:
                    catalog.setdefault("supports_streaming", True)
            elif isinstance(info, dict) and hosting == "litellm_proxy":
                # Proxy model aliases can describe an underlying model's capabilities,
                # but never establish the proxy's prices or deployment token limits.
                catalog = {
                    key: value
                    for key, value in normalize_model_metadata(info).items()
                    if key.startswith("supports_")
                    or key in {"supported_openai_params", "reasoning_efforts"}
                }
                status = "model_capabilities_only"
            else:
                status = "provider_mismatch"
        except Exception:
            # Catalog misses are expected for newly published and private models.
            status = "lookup_failed"
    normalized_remote = normalize_model_metadata(remote or {})
    result = {**catalog, **normalized_remote}
    if catalog.get("pricing") or normalized_remote.get("pricing"):
        result["pricing"] = {**catalog.get("pricing", {}), **normalized_remote.get("pricing", {})}
    sources = {key: f"catalog:{hosting}" for key in catalog}
    sources.update({key: "provider" for key in normalized_remote})
    if result.get("supports_reasoning") is True and "reasoning_efforts" not in result:
        result["reasoning_efforts"] = reasoning_efforts_for_model(
            model_id,
            provider_preset=preset,
            model_info=ModelInfo.model_validate({"model_id": model_id, **result}),
            supports_reasoning=True,
        )
        sources["reasoning_efforts"] = "model_policy"
    result["provider_metadata"] = {
        "catalog_provider": hosting,
        "catalog_status": status,
        "field_sources": sources,
    }
    return result
