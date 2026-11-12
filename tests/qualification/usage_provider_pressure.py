"""Process-isolated lower-provider comparison and enabled factory timing."""

from __future__ import annotations

import json
import os
import resource
import statistics
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from cognis.core.usage.maintenance import UsageMaintenanceService
from cognis.core.usage.price_publisher import UsagePricePinCache
from cognis.core.usage.projector import UsageProjector
from cognis.core.usage.service import UsageAccountingService
from cognis.models.usage import UsagePriceDefinition, UsagePriceRate
from cognis.models.usage_pricing import UsagePricingPin, usage_pricing_fingerprint
from cognis.ownership import SYSTEM_USER_EMAIL
from cognis.providers.auth.jwt import JWTAuthProvider
from cognis.providers.llm.litellm import LiteLLMProvider
from cognis.providers.llm.usage_capture import UsageCapture
from cognis.providers.registry import build_provider_registry
from cognis.store.database import create_engine, create_session_factory
from cognis.store.models import Base, LLMProvider
from cognis.store.usage.models import UsageAttemptRow
from tests.unit.test_provider_registry import _test_config
from tests.unit.test_usage_factory import capture_provider as capture_provider


@pytest.mark.asyncio
async def test_provider_timing(tmp_path, monkeypatch, capture_provider):
    """Run one mode per process so the installed observer never contaminates control."""
    mode = os.environ["COGNIS_USAGE_PERF_MODE"]
    assert mode in {"lower_control", "lower_capture", "factory_capture"}
    url = f"sqlite+aiosqlite:///{tmp_path / 'provider.db'}"
    engine = create_engine(url)
    sessions = create_session_factory(engine)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    config = _test_config(tmp_path, url)
    writer = UsageAccountingService(
        sessions, controller_owner_id=config.controller_id, projection_waker=lambda: None
    )
    pins = UsagePricePinCache()
    projector = UsageProjector(sessions, controller_owner_id=config.controller_id)
    maintenance = UsageMaintenanceService(
        sessions, projector=projector, controller_owner_id=config.controller_id
    )

    capture = UsageCapture(
        sink=writer,
        controller_owner_id=writer.controller_owner_id,
        coverage_epoch_id=writer.coverage_epoch_id,
        price_lookup=pins,
    )
    if mode == "factory_capture":
        auth = JWTAuthProvider(config.jwt_private_key_path, config.jwt_public_key_path)
        provider = build_provider_registry(config, sessions, auth, usage_capture=capture).llm
    else:
        provider = LiteLLMProvider(
            sessions, usage_capture=None if mode == "lower_control" else capture
        )
    provider_config = {
        "preset": "openai",
        "default_model": "gpt-4o-mini",
        "base_url": capture_provider[0],
        "api_base": capture_provider[0],
        "auth_config": {"mode": "env", "env_var": "COGNIS_FACTORY_TEST_API_KEY"},
    }
    monkeypatch.setenv("COGNIS_FACTORY_TEST_API_KEY", "factory-test-key")
    async with sessions.begin() as session:
        session.add(
            LLMProvider(
                provider_id="perf-provider",
                display_name="Performance fixture",
                owner_email=SYSTEM_USER_EMAIL,
                location="controller",
                backend="litellm",
                config=provider_config,
                status="active",
            )
        )
    info = await provider.get_model_info(
        model_id="gpt-4o-mini",
        provider_id="perf-provider",
        acting_user_email="perf-owner@usage.test",
    )
    assert info is not None
    fingerprint = usage_pricing_fingerprint(
        provider_id="perf-provider",
        model="gpt-4o-mini",
        model_info=info,
        provider_config=provider_config,
        service_tier="priority",
    )
    now = datetime.now(UTC)
    price = UsagePriceDefinition(
        revision_id="perf-price",
        source_revision="perf-snapshot",
        provider_id="perf-provider",
        model="gpt-4o-mini",
        effective_at=now - timedelta(days=1),
        recorded_at=now,
        currency="USD",
        basis="configured_estimate",
        provenance="configured",
        rates=(
            UsagePriceRate(dimension="input", per_million=Decimal(2), service_tier="priority"),
            UsagePriceRate(dimension="output", per_million=Decimal(4), service_tier="priority"),
        ),
    )
    await maintenance.publish_price(price)
    pins.replace(
        {
            ("perf-provider", "gpt-4o-mini", fingerprint): UsagePricingPin(
                price.revision_id, price.source_revision
            )
        }
    )
    await writer.start()
    samples = []
    cpu_before = time.process_time()
    try:
        for index in range(35):
            started = time.perf_counter_ns()
            result = await provider.generate(
                messages=[{"role": "user", "content": "PRIVATE PROMPT"}],
                model="gpt-4o-mini",
                provider_id="perf-provider",
                acting_user_email="perf-owner@usage.test",
                max_retries=0,
                service_tier="priority",
            )
            elapsed = (time.perf_counter_ns() - started) / 1_000_000
            assert result["choices"][0]["message"]["content"] == "PRIVATE OUTPUT"
            if index >= 5:
                samples.append(elapsed)
        cpu_seconds = time.process_time() - cpu_before
        await writer.drain()
        diagnostics = writer.diagnostics()
        async with sessions() as session:
            facts = await session.scalar(select(func.count()).select_from(UsageAttemptRow))
            priced = await session.scalar(
                select(func.count())
                .select_from(UsageAttemptRow)
                .where(UsageAttemptRow.captured_price_revision_id == price.revision_id)
            )
        assert facts == (0 if mode == "lower_control" else 35)
        assert priced == facts
        assert capture.rejected_events == 0
        assert capture.failed_events == 0
        assert len(capture_provider[1]) == 35
        ordered = sorted(samples)
        print(
            "provider_timing "
            + json.dumps(
                {
                    "mode": mode,
                    "warmup": 5,
                    "samples": len(samples),
                    "p50_ms": statistics.median(samples),
                    "p95_ms": ordered[28],
                    "cpu_seconds_including_warmup": cpu_seconds,
                    "maxrss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
                    "facts_including_warmup": facts,
                    "pinned_facts": priced,
                    "writer_payload_bytes": diagnostics.canonical_payload_bytes,
                    "rejected_events": capture.rejected_events,
                }
            )
        )
    finally:
        await writer.aclose()
        await projector.aclose()
        await engine.dispose()
