"""Canonical provider saves drive pricing; previews/rejected writes do not mutate the cache."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from sqlalchemy import select
from starlette.exceptions import HTTPException

from cognis.api.models import LLMProviderRequest, LLMProviderUpdateRequest
from cognis.core.management_access import ManagementActor
from cognis.core.provider_management import ProviderManagement
from cognis.core.usage.maintenance import UsageMaintenanceService
from cognis.core.usage.price_publisher import UsagePricePinCache, UsagePricePublisher
from cognis.core.usage.projector import UsageProjector
from cognis.models.usage_pricing import usage_pricing_fingerprint
from cognis.store.models import LLMProvider
from tests.integration.test_usage_query_api import ACTOR
from tests.integration.test_usage_query_api import db as db
from tests.unit.test_usage_storage_price_publisher import configured_info


@pytest.mark.asyncio
async def test_committed_provider_configuration_hooks_drive_real_publisher(db):
    actor = ManagementActor(ACTOR, "admin")
    projector = UsageProjector(db.factory, controller_owner_id="price_config_hook")
    maintenance = UsageMaintenanceService(
        db.factory, projector=projector, controller_owner_id="price_config_hook"
    )
    cache = UsagePricePinCache()

    async def resolve(model, provider_id=None, acting_user_email=None):
        assert acting_user_email == ACTOR and model == "gpt-4.1"
        async with db.factory() as session:
            row = await session.scalar(
                select(LLMProvider).where(LLMProvider.provider_id == provider_id)
            )
            return configured_info(row.config["models"][0]["input_cost_per_mtok"])

    publisher = UsagePricePublisher(
        db.factory, maintenance=maintenance, cache=cache, resolve_model=resolve
    )
    facade = SimpleNamespace(invalidate_runtime_capability_cache_for_provider=Mock())
    management = ProviderManagement(db.factory, facade, usage_price_publisher=publisher)
    config = {
        "preset": "openai",
        "models": [
            {
                "model_id": "gpt-4.1",
                "input_cost_per_mtok": 2,
                "output_cost_per_mtok": 8,
                "tier": "standard",
            }
        ],
    }

    def lookup(source, rate):
        return cache(
            provider_id="price_config_hook",
            model="gpt-4.1",
            metadata_fingerprint=usage_pricing_fingerprint(
                provider_id="price_config_hook",
                model="gpt-4.1",
                model_info=configured_info(rate),
                provider_config=source,
                service_tier="standard",
            ),
        )

    try:
        await publisher.start()
        assert await publisher.drain() and publisher.diagnostics.cached_pins == 0
        row = await management.create(
            actor,
            LLMProviderRequest(
                provider_id="price_config_hook", display_name="Pricing hook", config=config
            ),
        )
        assert row.config == config
        assert await publisher.drain()
        original = lookup(config, 2)
        assert original is not None

        changed = {
            **config,
            "models": [
                {
                    "model_id": "gpt-4.1",
                    "input_cost_per_mtok": 3,
                    "output_cost_per_mtok": 8,
                    "tier": "standard",
                }
            ],
        }
        await management.update(
            actor, row.provider_id, LLMProviderUpdateRequest(config=changed), preview=True
        )
        assert lookup(config, 2) == original
        with pytest.raises(HTTPException) as failure:
            await management.update(
                ManagementActor(ACTOR, "viewer"),
                row.provider_id,
                LLMProviderUpdateRequest(config=changed),
            )
        assert failure.value.status_code == 403 and lookup(config, 2) == original
        await management.update(actor, row.provider_id, LLMProviderUpdateRequest(config=changed))
        # The committed pin keeps pricing requests made with the old configuration
        # until the refresh publishes the new one; it never matches the new one.
        assert lookup(config, 2) == original
        assert lookup(changed, 3) is None
        assert await publisher.drain()
        replacement = lookup(changed, 3)
        assert replacement is not None and replacement != original
        assert lookup(changed, 2) is None
        assert lookup(config, 2) is None
        facade.invalidate_runtime_capability_cache_for_provider.assert_called_once_with(
            row.provider_id
        )
        await management.delete(actor, row.provider_id)
        assert await publisher.drain() and publisher.diagnostics.cached_pins == 0
        assert lookup(changed, 3) is None
    finally:
        await publisher.aclose()
        await projector.aclose()
