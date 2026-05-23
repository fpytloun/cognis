from __future__ import annotations

import pytest

from cognis.api.app import _check_startup_provider_health, _provider_health_note
from cognis.models.config import ProviderHealth


class _FlakyProvider:
    def __init__(self) -> None:
        self.calls = 0

    async def health(self) -> ProviderHealth:
        self.calls += 1
        if self.calls == 1:
            return ProviderHealth(name="mnemory", status="degraded", error="HTTP 401")
        return ProviderHealth(name="mnemory", status="healthy")


@pytest.mark.asyncio
async def test_startup_health_retries_until_healthy() -> None:
    provider = _FlakyProvider()

    health = await _check_startup_provider_health(provider, retry_delay_seconds=0)

    assert health.status == "healthy"
    assert provider.calls == 2


def test_provider_health_note_includes_http_details() -> None:
    health = ProviderHealth(
        name="mnemory",
        status="degraded",
        details={"status_code": 401, "body": "Unauthorized"},
    )

    assert _provider_health_note(health) == " (HTTP 401: Unauthorized)"
