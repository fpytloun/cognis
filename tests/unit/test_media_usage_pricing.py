"""Native media tariffs price measured quantities, never reservations or fake tokens."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from cognis.core.usage.maintenance import UsageMaintenanceScope
from cognis.core.usage.price_publisher import metadata_price
from cognis.core.usage.pricing import calculate_price
from cognis.models.config import ModelInfo
from cognis.models.usage import NormalizedUsage, UsageModelSelection
from cognis.store.usage.models import UsageAttemptRow, UsageValuationRow
from tests.usage_storage_harness import usage_storage as usage_storage


@pytest.mark.parametrize(
    ("mode", "operation", "pricing", "extra", "expected"),
    [
        (
            "image_generation",
            "image_generate",
            {"output_cost_per_image": 0.04},
            {"name": "output_image_count", "value": 2, "unit": "images"},
            "0.08",
        ),
        (
            "audio_transcription",
            "transcribe",
            {"input_cost_per_second": 0.0001, "output_cost_per_second": 0.0001},
            {"name": "input_audio_duration_ms", "value": 120000, "unit": "milliseconds"},
            "0.012",
        ),
        (
            "audio_speech",
            "synthesize",
            {"input_cost_per_character": 0.000015},
            {"name": "input_characters", "value": 1000, "unit": "characters"},
            "0.015",
        ),
        (
            "video",
            "video_generate",
            {"output_cost_per_second": 0.1},
            {"name": "output_video_duration_ms", "value": 8000, "unit": "milliseconds"},
            "0.8",
        ),
    ],
)
def test_native_media_prices(mode, operation, pricing, extra, expected):
    info = ModelInfo(
        model_id="media",
        model_mode=mode,
        pricing=pricing,
        provider_metadata={"field_sources": {"pricing": "catalog:openai"}},
    )
    _, price = metadata_price(
        provider_id="provider", model="media", info=info, config={}, now=datetime.now(UTC)
    )
    selection = UsageModelSelection(
        provider_id="provider",
        effective_model="media",
        streaming=False,
        operation=operation,
        source="adapter",
    )
    usage = NormalizedUsage(
        semantic_profile="openai",
        semantic_version=1,
        completeness="reported",
        cache_read_semantics="unknown",
        cache_write_semantics="unknown",
        reasoning_semantics="unknown",
        extras=(extra,),
    )
    result = calculate_price(usage, selection, price)
    assert result.cost == Decimal(expected)
    assert result.status == "complete"
    assert calculate_price(usage, selection, None).cost is None
    unknown = calculate_price(usage.model_copy(update={"extras": ()}), selection, price)
    assert unknown.cost is None and unknown.status == "unpriced"


def test_media_token_tariff_does_not_charge_alternate_duration_or_display_counts():
    info = ModelInfo(
        model_id="audio",
        model_mode="audio_speech",
        pricing={
            "input_cost_per_token": 0.000001,
            "output_cost_per_token": 0.000010,
            "output_cost_per_audio_token": 0.000012,
            "output_cost_per_second": 0.00025,
        },
        provider_metadata={"field_sources": {"pricing": "provider"}},
    )
    _, price = metadata_price(
        provider_id="provider", model="audio", info=info, config={}, now=datetime.now(UTC)
    )
    usage = NormalizedUsage(
        semantic_profile="openai",
        semantic_version=1,
        completeness="reported",
        input_tokens={"value": 100, "status": "reported", "source": "provider"},
        cache_read_semantics="unknown",
        cache_write_semantics="unknown",
        reasoning_semantics="unknown",
        extras=(
            {"name": "output_audio_tokens", "value": 50},
            {"name": "output_audio_duration_ms", "value": 60000, "unit": "milliseconds"},
        ),
    )
    selection = UsageModelSelection(
        provider_id="provider",
        effective_model="audio",
        streaming=True,
        operation="synthesize",
        source="adapter",
    )
    result = calculate_price(usage, selection, price)
    assert result.status == "complete"
    assert result.cost == Decimal("0.0007")


@pytest.mark.parametrize("amount", ["0", "0.123456"])
def test_provider_reported_cost_is_not_repriced(amount):
    usage = NormalizedUsage(
        semantic_profile="unknown",
        semantic_version=1,
        cache_read_semantics="unknown",
        cache_write_semantics="unknown",
        reasoning_semantics="unknown",
        provider_cost={"amount": amount},
    )
    selection = UsageModelSelection(
        provider_id="provider",
        effective_model="video",
        streaming=False,
        operation="video_generate",
        source="adapter",
    )
    result = calculate_price(usage, selection, None)
    assert result.cost == Decimal(amount)
    assert (result.status, result.currency, result.basis) == (
        "complete",
        "USD",
        "provider_reported",
    )


@pytest.mark.parametrize(
    "kind, mode", [("image", "image_generation"), ("audio", "audio_transcription")]
)
@pytest.mark.parametrize("cached", [100, None])
def test_cached_modality_tokens_are_not_charged_as_cached_text(kind, mode, cached):
    info = ModelInfo(
        model_id="mixed",
        model_mode=mode,
        pricing={
            "input_cost_per_token": 0.000001,
            f"input_cost_per_{kind}_token": 0.000010,
            "cache_read_input_token_cost": 0.0000005,
            f"cache_read_input_{kind}_token_cost": 0.000002,
        },
        provider_metadata={"field_sources": {"pricing": "provider"}},
    )
    _, price = metadata_price(
        provider_id="provider", model="mixed", info=info, config={}, now=datetime.now(UTC)
    )
    extras = [
        {"name": "input_text_tokens", "value": 1000},
        {"name": f"input_{kind}_tokens", "value": 200},
    ]
    if cached is not None:
        extras.append({"name": f"cache_read_{kind}_tokens", "value": cached})
    usage = NormalizedUsage(
        semantic_profile="openai",
        semantic_version=1,
        completeness="reported",
        input_tokens={"value": 1200, "status": "reported", "source": "provider"},
        cache_read_tokens={"value": 150, "status": "reported", "source": "provider"},
        cache_read_semantics="included_in_input",
        cache_write_semantics="unknown",
        reasoning_semantics="unknown",
        extras=extras,
    )
    selection = UsageModelSelection(
        provider_id="provider",
        effective_model="mixed",
        streaming=False,
        operation="image_generate" if kind == "image" else "transcribe",
        source="adapter",
    )
    result = calculate_price(usage, selection, price)
    # 950 fresh text + 50 cached text + 100 fresh modality + 100 cached modality.
    assert result.cost == (Decimal("0.002175") if cached is not None else None)
    assert result.status == ("complete" if cached is not None else "unpriced")


@pytest.mark.parametrize("known_price", [True, False])
async def test_native_quantities_remain_complete_after_archival_and_repricing(
    usage_storage, known_price
):
    h = usage_storage
    h.clock.now -= timedelta(days=40)
    event = h.event()
    info = ModelInfo(
        model_id=event.context.selection.effective_model,
        model_mode="image_generation",
        pricing={"output_cost_per_image": 0.04},
        provider_metadata={"field_sources": {"pricing": "catalog:openai"}},
    )
    _, price = metadata_price(
        provider_id=event.context.selection.provider_id,
        model=info.model_id,
        info=info,
        config={},
        now=h.clock.now,
    )
    if known_price:
        await h.maintenance.publish_price(price)
    event = event.model_copy(
        update={
            "context": event.context.model_copy(
                update={
                    "selection": event.context.selection.model_copy(
                        update={
                            "operation": "image_generate",
                            "captured_price_revision_id": price.revision_id
                            if known_price
                            else None,
                        }
                    )
                }
            ),
            "usage": NormalizedUsage(
                semantic_profile="openai",
                semantic_version=1,
                completeness="reported",
                cache_read_semantics="unknown",
                cache_write_semantics="unknown",
                reasoning_semantics="unknown",
                extras=({"name": "output_image_count", "value": 2, "unit": "images"},),
            ),
        }
    )
    h.writer.try_emit(event)
    await h.pump()
    async with h.sessions() as session:
        fact = (await session.scalars(select(UsageAttemptRow))).one()
        scope = UsageMaintenanceScope(owner_key=fact.owner_key, bucket_day=fact.bucket_day)
    job = await h.maintenance.request_archive(scope)
    await h.pump()
    assert (await h.maintenance.status(job.job_id)).status == "complete"
    _, revised = metadata_price(
        provider_id=event.context.selection.provider_id,
        model=info.model_id,
        info=info.model_copy(update={"pricing": {"output_cost_per_image": 0.08}}),
        config={},
        now=h.clock.now,
    )
    await h.maintenance.publish_price(revised)
    await h.pump()
    async with h.sessions() as session:
        assert await session.scalar(select(UsageAttemptRow)) is None
        value = (
            await session.scalars(
                select(UsageValuationRow).where(UsageValuationRow.source_kind == "archive")
            )
        ).one()
        assert value.historical_cost == (Decimal("0.08") if known_price else None)
        assert value.current_cost == Decimal("0.16")
        assert value.current_status == "complete"
