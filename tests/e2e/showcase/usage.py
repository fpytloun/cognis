"""Thirty days of fictional, priced usage attributed to the Lumen workspace.

The attempts are written through the canonical usage writer and projector, so
every Usage view, breakdown and flow reads them like real accounting. Rates are
configured estimates for fictional providers, not observed prices.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from cognis.core.usage.maintenance import UsageMaintenanceService
from cognis.core.usage.projector import UsageProjector
from cognis.core.usage.service import UsageAccountingService
from cognis.models.usage import (
    NormalizedUsage,
    UsageAttemptContext,
    UsageAttemptFinalized,
    UsageAttribution,
    UsageCount,
    UsageIdentity,
    UsageModelParameters,
    UsageModelSelection,
    UsagePerformance,
    UsagePriceDefinition,
    UsagePriceRate,
    UsageRequestStats,
    UsageResourceBinding,
    UsageTrace,
)
from cognis.store.queries import create_llm_provider

DAYS = 30
# provider, model, price revision, input $/M, output $/M
MODELS = {
    "pro": ("Northstar", "Northstar-Pro", "price_showcase_northstar_pro", "3", "15"),
    "mini": ("Northstar", "Northstar-Mini", "price_showcase_northstar_mini", "0.4", "1.6"),
    "chat": ("Southwind", "Southwind-Chat", "price_showcase_southwind_chat", "1", "4"),
    "local": ("Studio-Local", "Harbor-32B", "price_showcase_studio_local", "0", "0"),
}
# Which models each agent uses, in rotation.
AGENT_MODELS = {
    "atlas": ("mini", "pro", "mini"),
    "forge": ("pro", "pro", "mini"),
    "iris": ("chat", "chat", "pro"),
    "vela": ("mini", "local", "local"),
}
ATTEMPT_PREFIX = "attempt_showcase_usage_"


def _random(seed: int):
    """Small deterministic generator, so repeated seeds produce the same history."""
    state = seed & 0xFFFFFFFF
    while True:
        state = (1103515245 * state + 12345) & 0x7FFFFFFF
        yield state / 0x7FFFFFFF


def expected_attempts(items: list[dict]) -> int:
    """Number of attempts ``seed_usage`` writes for these work items."""
    return sum(len(_plan(item, index)) for index, item in enumerate(items))


def _plan(item: dict, index: int) -> list[tuple[int, int, float]]:
    """(day, ordinal, size) for one work item; heavier items run more often."""
    rng = _random(index * 7919 + 17)
    plan = []
    first = item.get("first_day", 0)
    for day in range(first, DAYS):
        weekday = (datetime.now(UTC) - timedelta(days=DAYS - 1 - day)).weekday()
        activity = 0.35 if weekday >= 5 else 1.0
        ramp = 0.6 + 0.8 * day / DAYS
        calls = int(item["weight"] * activity * ramp * (0.6 + next(rng)))
        plan.extend((day, ordinal, 0.5 + next(rng)) for ordinal in range(calls))
    return plan


async def seed_usage(
    factory: async_sessionmaker[AsyncSession],
    owner: str,
    items: list[dict],
) -> dict[str, str]:
    """Publish illustrative prices and project priced attempts for each work item.

    Each item has ``agent_id``, ``project_id``, ``weight`` (calls per busy day),
    ``tokens`` (typical input tokens) and either a conversation/session or a task.
    """
    today = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    start = today - timedelta(days=DAYS - 1)
    controller = "showcase_synthetic_usage"
    projector = UsageProjector(factory, controller_owner_id=controller)
    maintenance = UsageMaintenanceService(
        factory, projector=projector, controller_owner_id=controller
    )
    projector.attach_maintenance(maintenance)
    writer = UsageAccountingService(
        factory, controller_owner_id=controller, projection_waker=projector.wake
    )
    try:
        async with factory() as session:
            providers: dict[str, list[str]] = {}
            for provider, model, *_ in MODELS.values():
                providers.setdefault(provider, []).append(model)
            for provider, models in providers.items():
                await create_llm_provider(
                    session,
                    provider_id=provider,
                    display_name=provider.replace("-", " "),
                    location="controller",
                    backend="litellm",
                    owner_email=owner,
                    config={
                        "preset": "litellm_proxy",
                        "api_base": "http://mock-llm:8090/v1",
                        "default_model": models[0],
                        "models": [{"model_id": model, "display_name": model} for model in models],
                        "auth_config": {"mode": "env", "env_var": "COGNIS_LOCAL_LLM_API_KEY"},
                    },
                )
            await session.commit()
        for provider, model, revision, input_rate, output_rate in (
            *MODELS.values(),
            ("local-openai-compatible", "mock-model", "price_showcase_local_mock", "0", "0"),
        ):
            await maintenance.publish_price(
                UsagePriceDefinition(
                    revision_id=revision,
                    provider_id=provider,
                    model=model,
                    effective_at=start - timedelta(days=1),
                    recorded_at=today,
                    currency="USD",
                    basis="configured_estimate",
                    provenance="configured",
                    source_revision="showcase_illustrative_rates",
                    rates=(
                        UsagePriceRate(
                            dimension="input",
                            per_million=Decimal(input_rate),
                            service_tier="priority",
                        ),
                        UsagePriceRate(
                            dimension="output",
                            per_million=Decimal(output_rate),
                            service_tier="priority",
                        ),
                        UsagePriceRate(
                            dimension="cache_read",
                            per_million=Decimal(input_rate) / 10,
                            service_tier="priority",
                        ),
                    ),
                )
            )
        await writer.start()
        await projector.start()
        number = 0
        for index, item in enumerate(items):
            rotation = AGENT_MODELS[item["agent_id"]]
            for day, ordinal, size in _plan(item, index):
                number += 1
                provider, model, revision, _, _ = MODELS[rotation[(day + ordinal) % len(rotation)]]
                started = start + timedelta(
                    days=day, hours=8 + (ordinal * 37 + index * 11) % 11, minutes=ordinal * 7 % 60
                )
                if started > datetime.now(UTC):
                    started = datetime.now(UTC) - timedelta(minutes=number % 50 + 1)
                input_tokens = int(item["tokens"] * size)
                output_tokens = int(input_tokens * (0.08 + (ordinal % 5) * 0.03))
                elapsed = 900 + output_tokens // 2
                resources = UsageResourceBinding(
                    provenance="controller",
                    project_id=item["project_id"],
                    agent_id=item["agent_id"],
                    conversation_id=item.get("conversation_id"),
                    session_id=item.get("session_id"),
                    task_id=item.get("task_id"),
                )
                context = UsageAttemptContext(
                    trace=UsageTrace(
                        request_id=f"request_showcase_usage_{number}",
                        cycle_id=f"cycle_showcase_usage_{number}",
                        attempt_id=f"{ATTEMPT_PREFIX}{number}",
                        attempt_ordinal=0,
                    ),
                    started_at=started,
                    controller_owner_id=writer.controller_owner_id,
                    coverage_epoch_id=writer.coverage_epoch_id,
                    attribution=UsageAttribution(
                        identity=UsageIdentity(
                            principal_subject=owner,
                            principal_type="user",
                            usage_subject_kind="user",
                            usage_subject_id=owner,
                            routing_owner=owner,
                            provider_payer=owner,
                        ),
                        purpose="default",
                        resources=resources,
                    ),
                    selection=UsageModelSelection(
                        provider_id=provider,
                        effective_model=model,
                        streaming=True,
                        source="provider",
                        operation="chat",
                        captured_price_revision_id=revision,
                        service_tier="priority",
                        requested_parameters=UsageModelParameters(service_tier="priority"),
                        effective_parameters=UsageModelParameters(service_tier="priority"),
                    ),
                )
                event = UsageAttemptFinalized(
                    context=context,
                    observation_id=f"observation_showcase_usage_{number}",
                    outcome="success",
                    dispatch_status="provider_accepted",
                    finalized_at=started + timedelta(milliseconds=elapsed),
                    performance=UsagePerformance(source="provider", total_elapsed_ms=elapsed),
                    usage=NormalizedUsage(
                        semantic_profile="openai",
                        semantic_version=1,
                        completeness="reported",
                        input_tokens=UsageCount(
                            value=input_tokens, status="reported", source="provider"
                        ),
                        output_tokens=UsageCount(
                            value=output_tokens, status="reported", source="provider"
                        ),
                        cache_read_tokens=UsageCount(
                            value=input_tokens // 3, status="reported", source="provider"
                        ),
                        cache_write_tokens=UsageCount(
                            value=0, status="reported", source="provider"
                        ),
                        reasoning_tokens=UsageCount(value=0, status="reported", source="provider"),
                        cache_read_semantics="included_in_input",
                        cache_write_semantics="included_in_input",
                        reasoning_semantics="included_in_output",
                    ),
                )
                stats = UsageRequestStats(
                    observation_id=f"stats_showcase_usage_{number}",
                    request_id=context.trace.request_id,
                    attempt_id=context.trace.attempt_id,
                    cycle_id=context.trace.cycle_id,
                    request_started_at=started,
                    observed_at=event.finalized_at,
                    attribution=context.attribution,
                    controller_owner_id=writer.controller_owner_id,
                    coverage_epoch_id=writer.coverage_epoch_id,
                    stats_scope="server",
                    logical_outcome="success",
                    server={"elapsed_ms": elapsed},
                )
                # Stay well below the bounded queue: a rejected item is counted as a capture gap.
                if number % 200 == 0:
                    drained = await writer.drain(timeout_seconds=60)
                    if drained.pending_items or drained.writer_failed:
                        raise RuntimeError("Showcase usage writer did not drain")
                for observation in (event, stats):
                    if not writer.try_emit(observation):
                        raise RuntimeError("Showcase usage writer rejected an observation")
            await writer.drain(timeout_seconds=30)
        drained = await writer.drain(timeout_seconds=60)
        if drained.pending_items or drained.writer_failed:
            raise RuntimeError("Showcase usage writer did not drain")
        if not await projector.drain(timeout_seconds=600):
            raise RuntimeError("Showcase usage projector did not drain")
        return {
            "start": start.date().isoformat(),
            "end": today.date().isoformat(),
            "attempts": str(number),
        }
    finally:
        try:
            await writer.aclose()
        finally:
            await projector.aclose()
