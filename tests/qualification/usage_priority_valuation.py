"""Explicit qualification against the approved Storage pricing dependency.

Run this module explicitly in the integrated tree. It intentionally has no
fallback calculator, mocked normalized usage, or optional-import skip.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from cognis.core.usage.maintenance import UsageMaintenanceService
from cognis.core.usage.projector import UsageProjector
from cognis.core.usage.service import UsageAccountingService
from cognis.models.usage import UsageAttemptFinalized, UsagePriceDefinition, UsagePriceRate
from cognis.models.usage_pricing import UsagePricingPin, usage_pricing_fingerprint
from cognis.ownership import SYSTEM_USER_EMAIL
from cognis.providers.auth.jwt import JWTAuthProvider
from cognis.providers.llm.usage_capture import UsageCapture
from cognis.providers.registry import build_provider_registry
from cognis.store.database import create_session_factory
from cognis.store.models import Base, LLMProvider
from cognis.store.usage.models import UsageAttemptRow, UsageValuationRow
from tests.unit.test_provider_registry import _test_config
from tests.unit.test_usage_factory import capture_provider as capture_provider

pytest_plugins = ("tests.usage_storage_harness",)


@pytest.mark.parametrize("tier", ["omitted", None, "unknown", "auto", "priority"])
async def test_real_factory_priority_is_valued_and_unknown_tiers_are_not(
    tmp_path,
    monkeypatch,
    capture_provider,
    usage_storage,
    tier,
):
    engine = usage_storage.engine
    database_url = str(engine.url)
    sessions = create_session_factory(engine)
    async with engine.begin() as connection:
        await connection.run_sync(
            lambda sync_connection: Base.metadata.create_all(
                sync_connection, tables=[LLMProvider.__table__]
            )
        )
    config = _test_config(tmp_path, database_url)
    projector = UsageProjector(sessions, controller_owner_id=config.controller_id)
    maintenance = UsageMaintenanceService(
        sessions,
        projector=projector,
        controller_owner_id=config.controller_id,
    )
    projector.attach_maintenance(maintenance)
    accounting = UsageAccountingService(
        sessions,
        controller_owner_id=config.controller_id,
        projection_waker=projector.wake,
    )
    expected = {}

    def lookup(*, provider_id, model, metadata_fingerprint):
        return expected.get((provider_id, model, metadata_fingerprint))

    capture = UsageCapture(
        sink=accounting,
        controller_owner_id=accounting.controller_owner_id,
        coverage_epoch_id=accounting.coverage_epoch_id,
        price_lookup=lookup,
    )
    registry = build_provider_registry(
        config,
        sessions,
        JWTAuthProvider(config.jwt_private_key_path, config.jwt_public_key_path),
        usage_capture=capture,
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
                provider_id="factory-pricing",
                display_name="Factory pricing",
                owner_email=SYSTEM_USER_EMAIL,
                location="controller",
                backend="litellm",
                config=provider_config,
                status="active",
            )
        )
    now = datetime.now(UTC)
    price = UsagePriceDefinition(
        revision_id="factory-priority-revision",
        provider_id="factory-pricing",
        model="gpt-4o-mini",
        effective_at=now - timedelta(days=1),
        recorded_at=now,
        source_revision="factory-priority-snapshot",
        currency="USD",
        basis="configured_estimate",
        provenance="configured",
        rates=tuple(
            UsagePriceRate(dimension=dimension, per_million=rate, service_tier=rule_tier)
            for rule_tier in ("priority", "standard", None)
            for dimension, rate in (("input", Decimal(2)), ("output", Decimal(4)))
        ),
    )
    await maintenance.publish_price(price)
    info = await registry.llm.get_model_info(
        model_id="gpt-4o-mini",
        provider_id="factory-pricing",
        acting_user_email="factory-owner@example.com",
    )
    assert info is not None
    fingerprint = usage_pricing_fingerprint(
        provider_id="factory-pricing",
        model="gpt-4o-mini",
        model_info=info,
        provider_config=provider_config,
        service_tier="priority",
    )
    expected[("factory-pricing", "gpt-4o-mini", fingerprint)] = UsagePricingPin(
        price.revision_id,
        price.source_revision,
    )
    await accounting.start()
    try:
        result = await registry.llm.generate(
            messages=[{"role": "user", "content": "PRIVATE PROMPT"}],
            model="gpt-4o-mini",
            provider_id="factory-pricing",
            acting_user_email="factory-owner@example.com",
            max_retries=0,
            **({} if tier == "omitted" else {"service_tier": tier}),
        )
        assert result["choices"][0]["message"]["content"] == "PRIVATE OUTPUT"
        assert capture_provider[1] == ["/v1/responses"]
        expected_wire = "omitted" if tier is None else tier
        assert capture_provider[1].tiers == [expected_wire]
        await accounting.drain()
        for _ in range(64):
            maintained = await maintenance.process_page()
            projected = await projector.process_page()
            if not maintained and not projected:
                break
        else:
            raise AssertionError("Valuation did not converge within the bounded page allowance")
        # New session: qualify persisted canonical DTO and derived receipts, not
        # in-memory pins or a test-authored replacement observation.
        async with sessions() as session:
            row = (await session.scalars(select(UsageAttemptRow))).one()
            receipt = (await session.scalars(select(UsageValuationRow))).one()
            event = UsageAttemptFinalized.model_validate(
                {"context": row.context_payload, **row.terminal_payload},
            )
            selection = event.context.selection
            canonical = "priority" if tier == "priority" else None
            assert row.service_tier == selection.service_tier == canonical
            assert row.input_tokens == 2 and row.output_tokens == 1
            effective_tier = "auto" if tier == "auto" else canonical
            assert selection.effective_parameters.service_tier == effective_tier
            if tier == "priority":
                assert row.captured_price_revision_id == price.revision_id
                assert selection.resolver_snapshot_id == price.source_revision
                # Unknown cache-write semantics cannot invent disjoint input
                # billing. The known output subtotal is valued, explicitly partial.
                assert receipt.historical_cost == receipt.current_cost == Decimal("0.000004")
                assert receipt.historical_status == receipt.current_status == "partial"
            else:
                assert row.captured_price_revision_id is None
                assert receipt.historical_cost is receipt.current_cost is None
                assert receipt.historical_status == receipt.current_status == "unpriced"
    finally:
        await accounting.aclose()
        await projector.aclose()
