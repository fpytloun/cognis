"""Explicit request-owned transport attribution, never a dispatch capability."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

from cognis.models.config import ModelInfo
from cognis.models.usage import (
    UsageAttribution,
    UsageIdentity,
    UsageMediaOperation,
    UsageModelParameters,
    UsageServiceTierApplicability,
)
from cognis.providers.llm.usage_capture import UsageAttempt, UsageRequest

USAGE_BINDING_KEY = "_cognis_usage_binding"
USAGE_REQUEST_KEY = "_cognis_usage_request"


@dataclass(frozen=True)
class UsageBinding:
    """Bind one resolved target to a logical request without shared mutation."""

    request: UsageRequest
    provider_id: str
    model: str
    provider_payer: str | None = None
    model_info: ModelInfo | None = field(default=None, kw_only=True, repr=False)
    provider_config: Mapping[str, Any] | None = field(default=None, kw_only=True, repr=False)
    media_operation: UsageMediaOperation | None = field(default=None, kw_only=True)

    def attribution(self) -> UsageAttribution:
        """Bind the payer per target without rewriting the logical usage subject."""
        original = self.request.attribution
        if self.provider_payer is None:
            return original
        identity = UsageIdentity.model_validate(
            original.identity.model_dump() | {"provider_payer": self.provider_payer},
        )
        return original.model_copy(update={"identity": identity})

    def begin(
        self,
        *,
        operation: Literal["chat", "responses", "embeddings", "systemone", "native"],
        streaming: bool,
        parameters: Mapping[str, Any] | UsageModelParameters,
        semantic_profile: Literal["openai", "anthropic", "unknown"] = "unknown",
        applicability: UsageServiceTierApplicability | None = None,
        tier_omitted: bool = False,
    ) -> UsageAttempt | None:
        """Allocate observation state; malformed telemetry never gates inference."""
        try:
            attempt = self.request.begin(
                attribution=self.attribution(),
                selection=self.request.selection(
                    provider_id=self.provider_id,
                    model=self.model,
                    operation=self.media_operation or operation,
                    streaming=streaming,
                    wire_parameters=parameters,
                    model_info=self.model_info,
                    provider_config=self.provider_config,
                    applicability=applicability,
                    tier_omitted=tier_omitted,
                ),
                semantic_profile=semantic_profile,
            )
            attempt.on_generated = self.request.mark_generated
            return attempt
        except Exception:
            self.request.capture.failed_events += 1
            return None

    async def call(
        self,
        dispatch: Callable[[], Awaitable[Any]],
        *,
        operation: Literal["chat", "responses", "embeddings", "systemone", "native"],
        parameters: Mapping[str, Any],
        streaming: bool = False,
        semantic_profile: Literal["openai", "anthropic", "unknown"] = "unknown",
    ) -> Any:
        """Observe an actual submission; preserve the original result/exception."""
        try:
            selection = self.request.selection(
                provider_id=self.provider_id,
                model=self.model,
                operation=self.media_operation or operation,
                streaming=streaming,
                wire_parameters=parameters,
                model_info=self.model_info,
                provider_config=self.provider_config,
            )
            attribution = self.attribution()
        except Exception:
            self.request.capture.failed_events += 1
            return await dispatch()
        return await self.request.call(
            dispatch,
            selection=selection,
            semantic_profile=semantic_profile,
            attribution=attribution,
        )


def take_binding(kwargs: dict[str, Any], *, model: str | None = None) -> UsageBinding | None:
    """Remove process-local context before SDK serialization."""
    value = kwargs.pop(USAGE_BINDING_KEY, None)
    request = kwargs.pop(USAGE_REQUEST_KEY, None)
    provider_id = kwargs.pop("_cognis_usage_provider_id", None)
    if isinstance(value, UsageBinding):
        return value
    if isinstance(request, UsageRequest) and isinstance(provider_id, str):
        resolved_model = model or kwargs.get("model")
        if isinstance(resolved_model, str):
            return UsageBinding(request, provider_id, resolved_model)
    return None


def observed_sdk(
    sdk: Callable[..., Awaitable[Any]],
    binding: UsageBinding | None,
    *,
    operation: Literal["chat", "responses", "embeddings", "systemone", "native"],
) -> Callable[..., Awaitable[Any]]:
    """Bind physical observation without changing SDK or controller retry policy."""

    async def dispatch(**kwargs: Any) -> Any:
        wire = {
            key: value
            for key, value in kwargs.items()
            if not key.startswith(("cognis_", "_cognis_"))
        }
        if binding is None:
            return await sdk(**wire)
        from cognis.providers.llm.usage_sdk import observe_sdk

        return await observe_sdk(
            lambda: sdk(**wire),
            binding=binding,
            operation=operation,
            parameters=wire,
            dispatch_source=sdk,
        )

    return dispatch
