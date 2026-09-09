"""Shared Ollama model discovery helpers.

These helpers are intentionally limited to read-only Ollama endpoints:
``GET /api/tags`` and ``POST /api/show``.  They are used from both the
controller-side provider and executor-side discovery RPC so Cognis derives the
same model metadata regardless of where Ollama is reachable.
"""

from __future__ import annotations

import json
from typing import Any

import httpx


def is_ollama_remote(preset: str, base_url: str) -> bool:
    return preset.strip().lower() == "ollama" or "ollama" in base_url.lower()


def ollama_base_url(base_url: str) -> str:
    return base_url.rstrip("/") or "http://localhost:11434"


def ollama_model_name(model_id: str) -> str:
    return model_id.split("/", 1)[1] if model_id.startswith("ollama/") else model_id


def ollama_model_id(model_name: str) -> str:
    return model_name if model_name.startswith("ollama/") else f"ollama/{model_name}"


def _json_safe_model_value(value: Any) -> Any:
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, dict):
        return {str(key): _json_safe_model_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_safe_model_value(item) for item in value]
    try:
        dumped = json.dumps(value, default=str)
        return json.loads(dumped)
    except Exception:
        return str(value)


def _coerce_positive_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _ollama_max_context_window(show: dict[str, Any]) -> tuple[int | None, str | None]:
    model_info = show.get("model_info")
    if not isinstance(model_info, dict):
        return None, None
    preferred_suffixes = (".context_length", ".max_context_length")
    for suffix in preferred_suffixes:
        for key, value in model_info.items():
            if str(key).endswith(suffix):
                parsed = _coerce_positive_int(value)
                if parsed is not None:
                    return parsed, f"model_info.{key}"
    for key in ("context_length", "max_context_length", "num_ctx"):
        parsed = _coerce_positive_int(model_info.get(key))
        if parsed is not None:
            return parsed, f"model_info.{key}"
    return None, None


def _ollama_parameters(show: dict[str, Any]) -> dict[str, str]:
    parameters = show.get("parameters")
    if isinstance(parameters, dict):
        return {str(key): str(value) for key, value in parameters.items()}
    if not isinstance(parameters, str):
        return {}

    parsed: dict[str, str] = {}
    for raw_line in parameters.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        key, _, value = line.partition(" ")
        if key and value:
            parsed[key] = value.strip()
    return parsed


def _ollama_runtime_context_window(show: dict[str, Any]) -> tuple[int, str]:
    num_ctx = _coerce_positive_int(_ollama_parameters(show).get("num_ctx"))
    if num_ctx is not None:
        return num_ctx, "parameters.num_ctx"
    # Ollama's API default is 2048 when neither the Modelfile nor request
    # overrides num_ctx.  Use that conservative value only when /api/show
    # succeeded but did not report a Modelfile num_ctx.
    return 2048, "ollama.default.num_ctx"


def normalize_ollama_model_info(
    model_id: str,
    *,
    tag: dict[str, Any] | None = None,
    show: dict[str, Any] | None = None,
) -> dict[str, Any]:
    tag = tag or {}
    show = show or {}
    model_name = ollama_model_name(model_id)
    normalized: dict[str, Any] = {
        "model_id": model_id,
        "name": model_name,
        "display_name": model_name,
        "source": "ollama",
        "confidence": "live" if show else "discovered",
    }
    max_context_window, max_context_source = _ollama_max_context_window(show)
    context_window: int | None = None
    context_source: str | None = None
    if show:
        context_window, context_source = _ollama_runtime_context_window(show)
    if max_context_window is not None:
        normalized["max_context_window"] = max_context_window
        if context_window is not None:
            context_window = min(context_window, max_context_window)
    if context_window is not None:
        normalized["context_window"] = context_window
        normalized["max_input_tokens"] = context_window
        normalized["runtime_metadata"] = {
            "provider": "ollama",
            "model_name": model_name,
            "num_ctx": context_window,
            "context_source": context_source,
        }
    capabilities = show.get("capabilities")
    if isinstance(capabilities, list):
        capability_values = {str(item).lower() for item in capabilities}
        normalized["supports_tools"] = "tools" in capability_values
        normalized["supports_vision"] = "vision" in capability_values
        normalized["supports_embedding"] = "embedding" in capability_values
        normalized["supports_streaming"] = (
            "completion" in capability_values or "chat" in capability_values
        )
    elif "embed" in model_name.lower():
        normalized["supports_embedding"] = True
    details = show.get("details") if isinstance(show.get("details"), dict) else tag.get("details")
    provider_metadata: dict[str, Any] = {
        "provider": "ollama",
        "source": "ollama:/api/tags+show" if show else "ollama:/api/tags",
        "name": model_name,
    }
    for key in ("digest", "size", "modified_at"):
        if tag.get(key) is not None:
            provider_metadata[key] = tag[key]
    if isinstance(details, dict):
        provider_metadata["details"] = details
    if isinstance(show.get("parameters"), (str, dict)):
        provider_metadata["parameters"] = show["parameters"]
    if isinstance(capabilities, list):
        provider_metadata["capabilities"] = [str(item) for item in capabilities]
    model_info = show.get("model_info")
    if isinstance(model_info, dict):
        provider_metadata["model_info"] = model_info
    if max_context_source is not None:
        provider_metadata["max_context_source"] = max_context_source
    normalized["provider_metadata"] = _json_safe_model_value(provider_metadata)
    return normalized


async def fetch_ollama_show(
    client: httpx.AsyncClient,
    *,
    ollama_url: str,
    model_name: str,
    headers: dict[str, str],
) -> dict[str, Any]:
    response = await client.post(
        f"{ollama_url}/api/show",
        headers=headers,
        json={"model": model_name},
    )
    response.raise_for_status()
    data = response.json()
    return data if isinstance(data, dict) else {}


async def discover_ollama_models(
    *,
    base_url: str,
    api_key: str = "",
    timeout: float = 15.0,
    logger: Any | None = None,
) -> list[dict[str, Any]]:
    """Discover installed Ollama models through read-only metadata endpoints."""

    headers: dict[str, str] = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    ollama_url = ollama_base_url(base_url)
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.get(f"{ollama_url}/api/tags", headers=headers)
        response.raise_for_status()
        data = response.json()
        discovered: list[dict[str, Any]] = []
        raw_models = data.get("models", []) if isinstance(data, dict) else []
        for raw_model in raw_models:
            if not isinstance(raw_model, dict):
                continue
            raw_name = raw_model.get("name")
            if not isinstance(raw_name, str) or not raw_name.strip():
                continue
            model_name = raw_name.strip()
            model_id = ollama_model_id(model_name)
            show: dict[str, Any] = {}
            try:
                show = await fetch_ollama_show(
                    client,
                    ollama_url=ollama_url,
                    model_name=model_name,
                    headers=headers,
                )
            except Exception:
                if logger is not None:
                    logger.debug(
                        "Ollama /api/show failed during model discovery",
                        extra={"extra_data": {"model": model_name, "base_url": ollama_url}},
                        exc_info=True,
                    )
            discovered.append(normalize_ollama_model_info(model_id, tag=raw_model, show=show))
        return discovered
