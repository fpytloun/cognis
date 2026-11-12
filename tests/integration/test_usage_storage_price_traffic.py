"""Real factory/adapter traffic characterizes omitted, unknown and concrete wire tiers."""

from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import select

from cognis.core.usage.runtime import UsageRuntime
from cognis.models.usage_pricing import usage_pricing_fingerprint
from cognis.providers.auth.jwt import JWTAuthProvider
from cognis.providers.registry import build_provider_registry
from cognis.store.models import LLMProvider
from cognis.store.usage.models import UsageAttemptRow, UsageDailyTotalRow, UsageValuationRow
from tests.integration.test_usage_query_api import ACTOR
from tests.integration.test_usage_query_api import db as db
from tests.unit.test_provider_registry import _test_config
from tests.unit.test_usage_factory import capture_provider as capture_provider


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("send_tier", "wire_tier", "expected_tier"),
    [
        (False, None, "standard"),
        (True, None, None),
        (True, "auto", None),
        (True, "unknown", None),
        (True, "priority", "priority"),
    ],
)
async def test_normal_factory_price_traffic_exposes_tier_applicability(
    db,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capture_provider,
    send_tier: bool,
    wire_tier: str | None,
    expected_tier: str | None,
) -> None:
    config = _test_config(tmp_path, str(db.engine.url))
    auth = JWTAuthProvider(config.jwt_private_key_path, config.jwt_public_key_path)
    runtime = UsageRuntime(db.factory, controller_owner_id=config.controller_id)
    monkeypatch.setenv("COGNIS_PRICE_TRAFFIC_API_KEY", "price-traffic-test-key")
    provider_config = {
        "preset": "openai",
        "default_model": "gpt-4o-mini",
        "base_url": capture_provider[0],
        "api_base": capture_provider[0],
        "auth_config": {"mode": "env", "env_var": "COGNIS_PRICE_TRAFFIC_API_KEY"},
        "models": [
            {
                "model_id": "gpt-4o-mini",
                "input_cost_per_mtok": 2,
                "output_cost_per_mtok": 4,
                "pricing": {"input_cost_per_token": 0.000002, "output_cost_per_token": 0.000004},
            }
        ],
    }
    if wire_tier == "priority":
        provider_config["models"][0]["tier"] = "priority"
    async with db.factory.begin() as session:
        session.add(
            LLMProvider(
                provider_id="price_traffic",
                display_name="Price traffic",
                owner_email=ACTOR,
                location="controller",
                backend="litellm",
                config=provider_config,
                status="active",
            )
        )
    await runtime.start()
    registry = build_provider_registry(config, db.factory, auth, usage_capture=runtime.capture)
    try:
        assert runtime.capture.price_lookup is runtime.price_lookup is runtime.price_cache
        info = await registry.llm.get_model_info(
            "gpt-4o-mini",
            provider_id="price_traffic",
            acting_user_email=ACTOR,
        )
        assert info.input_cost_per_mtok == 2 and info.output_cost_per_mtok == 4
        if wire_tier == "priority":
            assert info.tier == provider_config["models"][0]["tier"] == "priority"
        else:
            assert "tier" not in provider_config["models"][0]
            assert info.tier == "standard"  # A metadata default, not wire applicability evidence.
        await runtime.start_pricing(resolve_model=registry.llm.get_model_info)
        assert await runtime.price_publisher.drain()
        committed = runtime.price_lookup(
            provider_id="price_traffic",
            model="gpt-4o-mini",
            metadata_fingerprint=usage_pricing_fingerprint(
                provider_id="price_traffic",
                model="gpt-4o-mini",
                model_info=info,
                provider_config=provider_config,
                service_tier=expected_tier,
            ),
        )
        assert (committed is not None) == (expected_tier is not None)
        parameters = {"service_tier": wire_tier} if send_tier else {}
        result = await registry.llm.generate(
            messages=[{"role": "user", "content": "PRIVATE PROMPT"}],
            model="gpt-4o-mini",
            provider_id="price_traffic",
            acting_user_email=ACTOR,
            max_retries=0,
            **parameters,
        )
        assert result["choices"][0]["message"]["content"] == "PRIVATE OUTPUT"
        assert capture_provider[1] == ["/v1/responses"]
        assert await runtime.service.drain()
        assert await runtime.projector.drain()
        async with db.factory() as session:
            attempt = (
                await session.scalars(
                    select(UsageAttemptRow).where(UsageAttemptRow.provider_id == "price_traffic")
                )
            ).one()
            valuation = (await session.scalars(select(UsageValuationRow))).one()
            daily = (
                await session.scalars(
                    select(UsageDailyTotalRow).where(
                        UsageDailyTotalRow.provider_id == "price_traffic",
                        UsageDailyTotalRow.finalized_count > 0,
                    )
                )
            ).one()
        selection = attempt.context_payload["selection"]
        assert selection["effective_parameters"].get("service_tier") == (
            None if wire_tier == "unknown" else wire_tier
        )
        assert selection.get("service_tier") == expected_tier
        if expected_tier is not None:
            assert committed is not None
            assert selection["captured_price_revision_id"] == committed.captured_price_revision_id
            assert selection["resolver_snapshot_id"] == committed.resolver_snapshot_id
            # The loopback peer reports two input tokens and one output token.
            assert valuation.historical_cost == Decimal("0.000008")
            assert valuation.current_cost == Decimal("0.000008")
            assert valuation.historical_status == valuation.current_status == "complete"
        else:
            assert selection.get("captured_price_revision_id") is None
            assert selection.get("resolver_snapshot_id") is None
            assert valuation.historical_cost is None and valuation.historical_status == "unpriced"
            assert valuation.current_cost is None and valuation.current_status == "unpriced"
        assert daily.finalized_count == 1
        assert (
            daily.historical_priced_count
            == daily.current_priced_count
            == (1 if expected_tier is not None else 0)
        )
        assert daily.historical_partial_count == daily.current_partial_count == 0
    finally:
        await registry.llm.aclose()
        await runtime.aclose()
