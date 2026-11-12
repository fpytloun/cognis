"""Provider monitoring policy and non-generating connectivity probes."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any, Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, model_validator

from cognis.providers.llm.bedrock import discover_bedrock_models
from cognis.providers.llm.hosted import PROVIDER_BASES, api_endpoint
from cognis.store.models import LLMProvider


class MonitoringOptions(BaseModel):
    """Per-provider automatic checks and remaining-quota thresholds."""

    model_config = ConfigDict(extra="forbid")
    enabled: bool = True
    interval_minutes: int = Field(default=15, ge=1, le=10080)
    warning_percent: float = Field(default=20, ge=0, le=100)
    critical_percent: float = Field(default=0, ge=0, le=100)

    @model_validator(mode="after")
    def ordered_thresholds(self) -> MonitoringOptions:
        if self.critical_percent > self.warning_percent:
            raise ValueError("Critical remaining quota must not exceed warning remaining quota")
        return self


def monitoring_options(provider: LLMProvider) -> MonitoringOptions:
    """Resolve saved options, including defaults for existing providers."""
    return MonitoringOptions.model_validate((provider.config or {}).get("monitoring", {}))


def provider_fingerprint(provider: LLMProvider) -> str:
    """Invalidate observations when routing, credentials, or ownership changes."""
    config = {key: value for key, value in (provider.config or {}).items() if key != "monitoring"}
    value = [config, provider.location, provider.backend, provider.owner_email]
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def usage_severity(
    usage: dict[str, Any] | None, options: MonitoringOptions, *, now: datetime
) -> Literal["warning", "critical"] | None:
    """Evaluate unexpired, sufficiently recent quota evidence."""
    if not usage or not usage.get("ok"):
        return None
    stamp = (
        usage.get("observed_at")
        if usage.get("source") == "anthropic_rate_limit_headers"
        else usage.get("fetched_at")
    )
    try:
        observed = as_utc(datetime.fromisoformat(str(stamp)))
    except ValueError:
        return None
    max_age = (
        60
        if usage.get("source") == "anthropic_rate_limit_headers"
        else max(3600, options.interval_minutes * 60)
    )
    if (now - observed).total_seconds() > max_age:
        return None
    containers = [usage, *usage.get("additional_rate_limits", [])]
    severity: Literal["warning", "critical"] | None = None
    for container in containers:
        windows = [container.get("primary"), container.get("secondary")]
        live_windows = []
        for window in windows:
            if not window:
                continue
            reset = window.get("resets_at")
            if reset:
                try:
                    if as_utc(datetime.fromisoformat(str(reset))) <= now:
                        continue
                except ValueError:
                    continue
            live_windows.append(window)
            percent = window.get("used_percent")
            if not isinstance(percent, (float, int)):
                continue
            remaining = max(0, 100 - percent)
            if remaining <= options.critical_percent:
                return "critical"
            if remaining <= options.warning_percent:
                severity = "warning"
        aggregate_headers = (
            container is usage
            and usage.get("source") == "anthropic_rate_limit_headers"
            and bool(usage.get("additional_rate_limits"))
        )
        if (
            not aggregate_headers
            and (container.get("limit_reached") or container.get("allowed") is False)
            and (live_windows or not any(windows))
        ):
            return "critical"
    return severity


async def probe_connectivity(
    provider: LLMProvider, request_kwargs: dict[str, Any]
) -> dict[str, str]:
    """Check one read-only metadata endpoint; never generate or discover all models."""
    if provider.location != "controller":
        return {"status": "unknown", "detail": "Executor-hosted connectivity check unsupported"}
    config = provider.config or {}
    preset = str(config.get("preset", ""))
    if preset == "bedrock":
        try:
            await discover_bedrock_models(
                region=request_kwargs.get("aws_region_name"),
                profile=request_kwargs.get("aws_profile_name"),
                include_profiles=False,
            )
        except Exception:
            return {
                "status": "error",
                "detail": "Bedrock catalog access failed; check runtime IAM permissions and region",
            }
        return {"status": "healthy", "detail": "Bedrock catalog reachable; no generation performed"}
    defaults = {
        **PROVIDER_BASES,
        "openai": "https://api.openai.com/v1",
        "anthropic": "https://api.anthropic.com",
        "ollama": "http://localhost:11434",
    }
    base = str(
        request_kwargs.get("api_base") or request_kwargs.get("base_url") or defaults.get(preset, "")
    ).rstrip("/")
    if not base or preset not in {
        "openai",
        "openai_compatible",
        "anthropic",
        "ollama",
        "litellm_proxy",
        *PROVIDER_BASES,
    }:
        return {"status": "unknown", "detail": "Non-generating connectivity check unsupported"}
    headers = dict(request_kwargs.get("extra_headers") or {})
    key = request_kwargs.get("api_key")
    if preset == "anthropic":
        headers.update({"anthropic-version": "2023-06-01"})
        if key:
            headers["x-api-key"] = str(key)
    elif preset == "elevenlabs" and key:
        headers["xi-api-key"] = str(key)
    elif preset == "gemini" and key:
        headers["x-goog-api-key"] = str(key)
    elif key:
        headers["Authorization"] = f"Bearer {key}"
    from cognis.providers.llm.compatible_transport import openai_endpoint

    url = base + "/api/tags" if preset == "ollama" else openai_endpoint(base, "models")
    if preset == "gemini":
        url = api_endpoint(base, "models", "v1beta")
    async with (
        httpx.AsyncClient(timeout=8, follow_redirects=False) as client,
        client.stream("GET", url, headers=headers) as response,
    ):
        if response.status_code in {401, 403}:
            return {"status": "error", "detail": "Provider authentication rejected"}
        if response.status_code == 429:
            return {"status": "warning", "detail": "Provider metadata endpoint rate limited"}
        if response.status_code in {404, 405}:
            return {"status": "unknown", "detail": "Provider metadata endpoint unsupported"}
        response.raise_for_status()
    return {"status": "healthy", "detail": "Metadata endpoint reachable; no generation performed"}
