"""Typed executor-local inference capability policy."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, StrictBool

from cognis.models.local_models import OllamaRuntimeCapability, OllamaRuntimeConfig


class ExecutorLocalInferenceConfig(BaseModel):
    """Resolved executor settings with backwards-compatible defaults."""

    model_config = ConfigDict(extra="forbid")

    local_inference_enabled: StrictBool = True
    ollama_runtime: OllamaRuntimeConfig = Field(default_factory=OllamaRuntimeConfig)

    @property
    def ollama_management_enabled(self) -> bool:
        """Return the effective managed-model mutation switch."""

        return self.local_inference_enabled and self.ollama_runtime.management_enabled


def resolve_executor_local_inference_config(
    config: dict[str, Any] | None,
) -> ExecutorLocalInferenceConfig:
    """Validate and resolve the local-inference subset of executor config."""

    raw = config if isinstance(config, dict) else {}
    ollama_raw = raw.get("ollama_runtime")
    return ExecutorLocalInferenceConfig.model_validate(
        {
            **(
                {"local_inference_enabled": raw["local_inference_enabled"]}
                if "local_inference_enabled" in raw
                else {}
            ),
            **({"ollama_runtime": ollama_raw} if ollama_raw is not None else {}),
        }
    )


def executor_local_inference_enabled(row: Any) -> bool:
    """Resolve persisted desired local-inference eligibility for an executor row."""

    return resolve_executor_local_inference_config(
        row.config if isinstance(getattr(row, "config", None), dict) else {}
    ).local_inference_enabled


def executor_local_inference_config_confirmed(row: Any) -> bool:
    """Require the persisted generation and executor advertisement to agree."""

    desired = resolve_executor_local_inference_config(
        row.config if isinstance(getattr(row, "config", None), dict) else {}
    )
    desired_version = int(getattr(row, "desired_config_version", 0) or 0)
    applied_version = int(getattr(row, "applied_config_version", 0) or 0)
    if desired_version != applied_version or getattr(row, "runtime_state", None) not in {
        "active",
        "degraded",
    }:
        return False
    metadata = getattr(row, "runtime_metadata", None)
    if not isinstance(metadata, dict):
        return False
    if metadata.get("local_inference_enabled") is not desired.local_inference_enabled:
        return False
    try:
        advertised = OllamaRuntimeCapability.model_validate(metadata.get("ollama_runtime"))
    except Exception:
        return False
    return (
        advertised.port == desired.ollama_runtime.port
        and advertised.endpoint == desired.ollama_runtime.endpoint
        and advertised.management_enabled is desired.ollama_management_enabled
    )


def executor_local_inference_config_status(row: Any) -> str:
    """Expose a stable desired/applying/confirmed status for API clients."""

    if executor_local_inference_config_confirmed(row):
        return "confirmed"
    if int(getattr(row, "desired_config_version", 0) or 0) != int(
        getattr(row, "applied_config_version", 0) or 0
    ) or getattr(row, "runtime_state", None) in {"reconfiguring", "stale"}:
        return "applying"
    return "unconfirmed"


def executor_local_inference_configured(row: Any) -> bool:
    """Return whether persisted configuration permits local inference."""

    return (
        getattr(row, "status", None) == "active"
        and getattr(row, "executor_type", None) == "websocket"
        and executor_local_inference_enabled(row)
    )


def executor_local_inference_eligible(
    row: Any,
    *,
    advertised: bool | None = None,
) -> bool:
    """Require active persisted intent and a confirmed executor generation."""

    return (
        executor_local_inference_configured(row)
        and executor_local_inference_config_confirmed(row)
        and advertised is not False
    )


def executor_local_inference_routable(
    row: Any,
    *,
    advertised: bool | None = None,
) -> bool:
    """Require a WebSocket executor that can accept controller inference RPCs."""

    return getattr(row, "executor_type", None) == "websocket" and executor_local_inference_eligible(
        row,
        advertised=advertised,
    )
