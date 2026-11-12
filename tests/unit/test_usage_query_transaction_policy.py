"""Dialect-local usage SQL policy and request-session cleanup contracts."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from cognis.core.usage import query_service
from cognis.core.usage.query_metadata import UsageIndexCapabilities
from cognis.core.usage.query_models import UsageSummaryRequest


@pytest.mark.asyncio
@pytest.mark.parametrize("dialect", ["postgresql", "sqlite"])
@pytest.mark.parametrize("inclusion", ["self", "inclusive"])
@pytest.mark.parametrize("failure", ["none", "authorization", "query"])
async def test_usage_sql_policy_precedes_authorization_and_releases_session(
    monkeypatch: pytest.MonkeyPatch, dialect: str, inclusion: str, failure: str
) -> None:
    session = AsyncMock(spec=AsyncSession)
    session.bind = SimpleNamespace(dialect=SimpleNamespace(name=dialect))
    session.__aenter__.return_value = session
    commands: list[str] = []

    async def execute(statement: Any) -> None:
        commands.append(str(statement))

    session.execute.side_effect = execute
    expected: list[str] = []
    if inclusion == "inclusive":
        expected.append(
            "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"
            if dialect == "postgresql"
            else "BEGIN"
        )
    if dialect == "postgresql":
        expected.append(
            "SELECT set_config('statement_timeout', '1000ms', true), set_config('jit', 'off', true)"
        )
    query = UsageSummaryRequest(
        items=[{"kind": "session", "id": "session_policy"}], inclusion=inclusion
    )
    # Authorized scope stand-in: the request applies filter and mode policy to it.
    scope = SimpleNamespace(mode="personal", check_filters=lambda _filters: None)
    refused = RuntimeError("request refused")

    async def authorize(actual_session: AsyncSession, **kwargs: Any) -> object:
        assert actual_session is session
        assert commands == expected
        assert kwargs == {
            "actor": "consumer@usage.test",
            "requested": query.scope,
            "invocation": None,
            "actor_role": None,
        }
        if failure == "authorization":
            raise refused
        return scope

    monkeypatch.setattr(query_service, "authorize_usage", authorize)
    service = query_service.UsageQueryService(
        lambda: session,
        indexes=UsageIndexCapabilities(prefixes={}),
        cursor_secret=b"usage-transaction-policy-test-secret",
    )

    async def request() -> None:
        async with service._request("consumer@usage.test", query, None, None) as result:
            assert result == (session, scope)
            if failure == "query":
                raise refused

    if failure == "none":
        await request()
    else:
        with pytest.raises(RuntimeError) as error:
            await request()
        assert error.value is refused
    assert commands == expected
    session.__aenter__.assert_awaited_once()
    session.__aexit__.assert_awaited_once()
    exit_args = session.__aexit__.await_args.args
    assert exit_args[1] is (None if failure == "none" else refused)
