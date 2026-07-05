"""Codex-specific model catalog, discovery, and usage helpers."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from cognis.models.config import normalize_reasoning_level

CODEX_MODELS_URL = "https://chatgpt.com/backend-api/codex/models"
CODEX_RESPONSES_URL = "https://chatgpt.com/backend-api/codex/responses"
CODEX_USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"
CODEX_USAGE_DASHBOARD_URL = "https://chatgpt.com/codex/settings/usage"
CODEX_CLIENT_VERSION = "0.124.0"
CODEX_MODEL_CACHE_TTL_SECONDS = 300.0

_CODEX_CATALOG: dict[str, dict[str, Any]] | None = None
_CODEX_MODEL_INFO_OVERRIDES: dict[str, dict[str, Any]] = {
    # Spark is not present in the upstream Codex catalog and the ChatGPT
    # Responses backend currently rejects requests just above 128k input
    # tokens with context_length_exceeded. Keep this as a Cognis runtime
    # correction rather than changing the upstream-derived catalog file.
    "gpt-5.3-codex-spark": {
        "context_window": 272_000,
        "max_input_tokens": 128_000,
        "max_context_window": 272_000,
        "max_output_tokens": 128_000,
    },
}
_CODEX_NATIVE_PDF_MODELS = {
    # Keep the downstreamed Codex catalog JSON intact. The upstream catalog
    # currently advertises text/image modalities only, while the Responses
    # transport accepts PDFs for these models.
    "gpt-5.5",
    "gpt-5.4",
    "gpt-5.4-mini",
    "gpt-5.3-codex-spark",
    "gpt-5.3-codex",
    "gpt-5.2",
}


@dataclass(frozen=True)
class CodexAuth:
    """Headers required by ChatGPT-backed Codex endpoints."""

    access_token: str
    account_id: str | None = None
    fedramp: bool = False

    @property
    def headers(self) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self.access_token}",
            "Originator": "codex_cli_rs",
            "User-Agent": f"codex_cli_rs/{CODEX_CLIENT_VERSION}",
        }
        if self.account_id:
            headers["ChatGPT-Account-ID"] = self.account_id
        if self.fedramp:
            headers["X-OpenAI-Fedramp"] = "true"
        return headers


def load_bundled_codex_catalog() -> dict[str, dict[str, Any]]:
    """Return bundled Codex model catalog entries keyed by model slug."""

    global _CODEX_CATALOG
    if _CODEX_CATALOG is not None:
        return _CODEX_CATALOG
    raw = (Path(__file__).parent / "data" / "codex_models.json").read_text()
    payload = json.loads(raw)
    models: dict[str, dict[str, Any]] = {}
    for item in payload.get("models", []):
        if not isinstance(item, dict):
            continue
        slug = item.get("slug")
        if isinstance(slug, str) and slug.strip():
            models[slug.strip()] = item
    _CODEX_CATALOG = models
    return models


def codex_catalog_model_info(model_id: str) -> dict[str, Any] | None:
    """Map one bundled Codex catalog entry into Cognis ModelInfo fields."""

    item = load_bundled_codex_catalog().get(model_id)
    if item is None:
        return None
    return _codex_model_info_from_entry(item, source="codex_catalog", confidence="bundled")


def codex_unknown_model_info(model_id: str) -> dict[str, Any]:
    """Conservative Codex metadata for user-specified models absent from the catalog."""

    supports_openai_apply_patch = "codex" in model_id.lower()
    return {
        "model_id": model_id,
        "display_name": model_id,
        "context_window": 400_000,
        "max_input_tokens": 272_000,
        "max_context_window": 400_000,
        "max_output_tokens": 128_000,
        "supports_tools": True,
        "supports_streaming": True,
        "supports_vision": True,
        "supports_reasoning": True,
        "reasoning_efforts": ["low", "medium", "high"],
        "supports_prompt_caching": True,
        "supports_tool_search": True,
        "supports_responses_api": True,
        "supports_openai_namespace_tools": True,
        "supports_openai_allowed_tools": True,
        "supports_openai_apply_patch": supports_openai_apply_patch,
        "openai_apply_patch_tool_type": "freeform" if supports_openai_apply_patch else None,
        "max_tools": 128,
        "tier": "codex",
        "source": "codex_unknown",
        "confidence": "fallback",
    }


def bundled_codex_model_entries(
    configured_models: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Return bundled Codex catalog entries plus configured unknown models."""

    entries = [
        _codex_model_info_from_entry(item, source="codex_catalog", confidence="bundled")
        for item in sorted(
            load_bundled_codex_catalog().values(),
            key=lambda model: int(model.get("priority") or 999),
        )
        if item.get("supported_in_api") is not False and item.get("visibility") != "hidden"
    ]
    seen = {entry["model_id"] for entry in entries}
    for model in configured_models or []:
        model_id = model.get("model_id") if isinstance(model, dict) else None
        if not isinstance(model_id, str) or not model_id.strip() or model_id in seen:
            continue
        merged = codex_unknown_model_info(model_id.strip())
        merged.update(model)
        merged["source"] = "configured"
        merged["confidence"] = "configured"
        entries.append(merged)
        seen.add(model_id.strip())
    return entries


async def fetch_codex_models(auth: CodexAuth, *, timeout: float = 15.0) -> list[dict[str, Any]]:
    """Fetch live Codex model metadata from ChatGPT's Codex backend."""

    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.get(
            CODEX_MODELS_URL,
            headers=auth.headers,
            params={"client_version": CODEX_CLIENT_VERSION},
        )
        response.raise_for_status()
    payload = response.json()
    models = payload.get("models") if isinstance(payload, dict) else None
    if not isinstance(models, list):
        return []
    entries: list[dict[str, Any]] = []
    for item in models:
        if isinstance(item, dict):
            entry = _codex_model_info_from_entry(item, source="codex_remote", confidence="live")
            if entry.get("model_id"):
                entries.append(entry)
    return entries


async def fetch_codex_usage(auth: CodexAuth, *, timeout: float = 15.0) -> dict[str, Any]:
    """Fetch Codex usage and limit windows from the ChatGPT backend."""

    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.get(CODEX_USAGE_URL, headers=auth.headers)
        response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise RuntimeError("Codex usage response was not an object")
    return _normalize_usage_payload(payload)


async def test_codex_responses(
    auth: CodexAuth,
    *,
    model: str,
    timeout: float = 15.0,
) -> None:
    """Make a minimal direct Codex Responses request for provider health checks.

    This intentionally bypasses LiteLLM's ChatGPT provider. LiteLLM may start
    its own device-code login when its process-local auth state is missing;
    provider tests must be non-interactive and use Cognis' encrypted OAuth token
    as the only credential source.
    """

    payload = {
        "model": model,
        "input": [{"role": "user", "content": "Say hello."}],
        "instructions": "You are a provider health check. Reply with a short greeting.",
        "store": False,
        "stream": True,
    }
    async with (
        httpx.AsyncClient(timeout=timeout) as client,
        client.stream("POST", CODEX_RESPONSES_URL, headers=auth.headers, json=payload) as response,
    ):
        response.raise_for_status()
        async for line in response.aiter_lines():
            if line.strip():
                return
    raise RuntimeError("Codex Responses health check returned an empty stream")


def _codex_model_info_from_entry(
    item: dict[str, Any], *, source: str, confidence: str
) -> dict[str, Any]:
    slug = str(item.get("slug") or "").strip()
    if not slug:
        return {}
    raw_modalities = item.get("input_modalities")
    modalities = raw_modalities if isinstance(raw_modalities, list) else []
    reasoning_efforts = _reasoning_efforts_from_catalog(item)
    supports_reasoning = bool(reasoning_efforts or item.get("default_reasoning_level"))
    supports_tool_search = bool(item.get("supports_search_tool"))
    input_modalities = [str(value).lower() for value in modalities]
    max_input_tokens = _positive_int(item.get("context_window"), 272_000)
    max_output_tokens = _positive_int(item.get("max_output_tokens"), 128_000)
    max_context_window = _positive_int(item.get("max_context_window"), max_input_tokens)
    context_window = max_input_tokens + max_output_tokens
    max_total_context_window = max_context_window + max_output_tokens
    info = {
        "model_id": slug,
        "display_name": item.get("display_name") or slug,
        "context_window": context_window,
        "max_input_tokens": max_input_tokens,
        "max_context_window": max_total_context_window,
        "max_output_tokens": max_output_tokens,
        "supports_tools": True,
        "supports_streaming": True,
        "supports_vision": "image" in input_modalities,
        "supports_pdf_input": (
            bool(item.get("supports_pdf_input"))
            or "pdf" in input_modalities
            or slug in _CODEX_NATIVE_PDF_MODELS
        ),
        "supports_file_input": bool(item.get("supports_file_input")) or "file" in input_modalities,
        "supports_reasoning": supports_reasoning,
        "reasoning_efforts": reasoning_efforts,
        "reasoning_summary_format": item.get("reasoning_summary_format"),
        "default_reasoning_summary": item.get("default_reasoning_summary"),
        "supports_verbosity": bool(item.get("support_verbosity")),
        "default_verbosity": item.get("default_verbosity"),
        "supports_prompt_caching": True,
        "supports_tool_search": supports_tool_search,
        "supports_responses_api": True,
        "supports_openai_namespace_tools": supports_tool_search,
        "supports_openai_allowed_tools": supports_tool_search,
        "supports_openai_apply_patch": bool(item.get("apply_patch_tool_type")),
        "openai_apply_patch_tool_type": item.get("apply_patch_tool_type"),
        "max_tools": 128,
        "tier": "codex",
        "source": source,
        "confidence": confidence,
        "description": item.get("description"),
        "visibility": item.get("visibility"),
        "supported_in_api": item.get("supported_in_api"),
        "available_in_plans": item.get("available_in_plans") or [],
    }
    info.update(_CODEX_MODEL_INFO_OVERRIDES.get(slug, {}))
    return info


def _reasoning_efforts_from_catalog(item: dict[str, Any]) -> list[str]:
    values: list[str] = []
    raw = item.get("supported_reasoning_levels")
    if isinstance(raw, list):
        for level in raw:
            effort = level.get("effort") if isinstance(level, dict) else level
            normalized = normalize_reasoning_level(str(effort)) if effort is not None else None
            if normalized and normalized not in {"default", "none"} and normalized not in values:
                values.append(normalized)
    return values


def _normalize_usage_payload(payload: dict[str, Any]) -> dict[str, Any]:
    raw_rate_limit = payload.get("rate_limit")
    rate_limit: dict[str, Any] = raw_rate_limit if isinstance(raw_rate_limit, dict) else {}
    credits = payload.get("credits") if isinstance(payload.get("credits"), dict) else None
    reached = payload.get("rate_limit_reached_type")
    if isinstance(reached, dict):
        reached_type = reached.get("type")
    elif isinstance(reached, str):
        reached_type = reached
    else:
        reached_type = None
    additional: list[dict[str, Any]] = []
    raw_additional = payload.get("additional_rate_limits")
    if isinstance(raw_additional, list):
        for item in raw_additional:
            if not isinstance(item, dict):
                continue
            raw_item_rate = item.get("rate_limit")
            item_rate: dict[str, Any] = raw_item_rate if isinstance(raw_item_rate, dict) else {}
            additional.append(
                {
                    "limit_id": item.get("metered_feature"),
                    "limit_name": item.get("limit_name"),
                    "primary": _normalize_usage_window(item_rate.get("primary_window")),
                    "secondary": _normalize_usage_window(item_rate.get("secondary_window")),
                    "allowed": item_rate.get("allowed"),
                    "limit_reached": item_rate.get("limit_reached"),
                }
            )
    return {
        "ok": True,
        "source": "chatgpt_codex_usage",
        "usage_url": CODEX_USAGE_DASHBOARD_URL,
        "fetched_at": datetime.now(UTC).isoformat(),
        "plan_type": payload.get("plan_type"),
        "primary": _normalize_usage_window(rate_limit.get("primary_window")),
        "secondary": _normalize_usage_window(rate_limit.get("secondary_window")),
        "credits": _normalize_credits(credits),
        "rate_limit_reached_type": reached_type,
        "allowed": rate_limit.get("allowed"),
        "limit_reached": rate_limit.get("limit_reached"),
        "additional_rate_limits": additional,
    }


def _normalize_usage_window(window: Any) -> dict[str, Any] | None:
    if not isinstance(window, dict):
        return None
    seconds = _positive_int(window.get("limit_window_seconds"), 0)
    used_percent = _float_or_none(window.get("used_percent"))
    reset_after_seconds = _positive_int_or_none(window.get("reset_after_seconds"))
    return {
        "used_percent": used_percent if used_percent is not None else 0.0,
        "window_duration_mins": (seconds + 59) // 60 if seconds > 0 else None,
        "resets_at": _timestamp_to_iso(window.get("reset_at")),
        "reset_after_seconds": reset_after_seconds,
    }


def _normalize_credits(credits: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(credits, dict):
        return None
    return {
        "has_credits": credits.get("has_credits"),
        "unlimited": credits.get("unlimited"),
        "balance": credits.get("balance"),
    }


def _timestamp_to_iso(value: Any) -> str | None:
    try:
        timestamp = int(value)
    except (TypeError, ValueError):
        return None
    if timestamp <= 0:
        return None
    return datetime.fromtimestamp(timestamp, tz=UTC).isoformat()


def _positive_int(value: Any, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _positive_int_or_none(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _float_or_none(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
