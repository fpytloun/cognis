"""Rejected provider OAuth refresh grants surface as reauthorization, once."""

from __future__ import annotations

import time
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from cognis import json_codec as json
from cognis.providers.llm import anthropic_subscription
from cognis.providers.llm import litellm as litellm_module
from cognis.providers.llm.errors import ProviderReauthorizationRequired, classify_llm_exception
from cognis.providers.llm.litellm import LiteLLMProvider
from cognis.store.database import create_engine, create_session_factory
from cognis.store.models import Base, LLMProvider

OWNER = "owner@cognis.test"
REAL_ASYNC_CLIENT = httpx.AsyncClient
TOKEN_SECRET = "llm_oauth_anthropic_claude"


class Secrets:
    """Owner-scoped store with the production user-then-system lookup order."""

    def __init__(self) -> None:
        self.values: dict[tuple[str, str], str] = {}

    async def get_secret(self, name: str, user_id: str, agent_id: str | None = None) -> str:
        for scope in ("user", "system"):
            if (user_id, f"{scope}:{name}") in self.values:
                return self.values[(user_id, f"{scope}:{name}")]
        raise KeyError(name)

    async def set_secret(self, name, value, user_id, scope="user", agent_id=None, description=None):
        self.values[(user_id, f"{scope}:{name}")] = value

    async def delete_secret(self, name, user_id, scope="user", agent_id=None) -> bool:
        return self.values.pop((user_id, f"{scope}:{name}"), None) is not None


@pytest.fixture
async def llm(tmp_path):
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path}/cognis.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sessions = create_session_factory(engine)
    async with sessions() as db:
        db.add(
            LLMProvider(
                provider_id="claude",
                display_name="Claude Max",
                location="controller",
                backend="litellm",
                owner_email=OWNER,
                config={
                    "preset": "anthropic",
                    "auth_config": {"mode": "oauth", "provider": "anthropic_subscription"},
                },
            )
        )
        await db.commit()
    secrets = Secrets()
    provider = LiteLLMProvider(sessions, secrets_provider=secrets)
    yield provider, secrets, sessions
    await engine.dispose()


def token_endpoint(monkeypatch, status: int) -> list[httpx.Request]:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if status == 200:
            return httpx.Response(
                200,
                json={"access_token": "fresh", "refresh_token": "next", "expires_in": 3600},
            )
        return httpx.Response(status, json={"error": "invalid_grant"})

    monkeypatch.setattr(
        anthropic_subscription.httpx,
        "AsyncClient",
        lambda **kwargs: REAL_ASYNC_CLIENT(transport=httpx.MockTransport(handler), **kwargs),
    )
    return requests


async def store_expired_token(secrets: Secrets) -> None:
    await secrets.set_secret(
        TOKEN_SECRET,
        json.dumps({"access_token": "old", "refresh_token": "rt", "expires_at": time.time() - 1}),
        OWNER,
    )


async def provider_row(sessions) -> LLMProvider:
    async with sessions() as db:
        row = await db.get(LLMProvider, "claude")
        assert row is not None
        return row


@pytest.mark.asyncio
async def test_rejected_refresh_requires_reauthorization_until_oauth_completes(llm, monkeypatch):
    provider, secrets, sessions = llm
    reported: list[str] = []
    provider.set_reauthorization_observer(reported.append)
    await store_expired_token(secrets)
    requests = token_endpoint(monkeypatch, 400)
    row = await provider_row(sessions)

    with pytest.raises(ProviderReauthorizationRequired) as first:
        await provider._anthropic_subscription_auth(row)
    payload = classify_llm_exception(first.value)
    assert payload["category"] == "authentication"
    assert payload["message"] == (
        "Claude Max sign-in expired or was revoked. Reauthorize the provider in "
        "Settings > Providers."
    )
    assert (await provider.get_anthropic_oauth_status("claude"))["status"] == (
        "reauthorization_required"
    )

    # The rejection is durable: later requests fail fast without the token endpoint.
    with pytest.raises(ProviderReauthorizationRequired):
        await provider._anthropic_subscription_auth(row)
    assert len(requests) == 1
    assert reported == ["claude", "claude"]

    # Completing the real OAuth flow writes a fresh record, which clears the state.
    token_endpoint(monkeypatch, 200)
    started = await provider.start_anthropic_oauth("claude")
    state = parse_qs(urlparse(started["authorization_url"]).query)["state"][0]
    completed = await provider.complete_anthropic_oauth("claude", f"code-1#{state}")
    assert completed["status"] == "authorized"
    assert (await provider.get_anthropic_oauth_status("claude"))["status"] == "authorized"
    auth = await provider._anthropic_subscription_auth(row)
    assert auth.access_token == "fresh"


@pytest.mark.asyncio
async def test_rejected_chatgpt_refresh_requires_reauthorization(llm, monkeypatch):
    provider, secrets, sessions = llm
    async with sessions() as db:
        db.add(
            LLMProvider(
                provider_id="codex",
                display_name="ChatGPT Pro",
                location="controller",
                backend="litellm",
                owner_email=OWNER,
                config={"preset": "chatgpt"},
            )
        )
        await db.commit()
        row = await db.get(LLMProvider, "codex")
    await secrets.set_secret(
        "llm_oauth_chatgpt_codex",
        json.dumps({"access_token": "old", "refresh_token": "rt", "expires_at": 1}),
        OWNER,
        scope="system",
    )
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(401, json={"error": "invalid_grant"})

    monkeypatch.setattr(
        litellm_module.httpx,
        "AsyncClient",
        lambda **kwargs: REAL_ASYNC_CLIENT(transport=httpx.MockTransport(handler), **kwargs),
    )
    for _ in range(2):
        with pytest.raises(ProviderReauthorizationRequired) as rejected:
            await provider._chatgpt_codex_auth(row)
    assert str(rejected.value).startswith("ChatGPT Pro sign-in expired or was revoked.")
    assert len(requests) == 1
    assert (await provider.get_chatgpt_oauth_status("codex"))["status"] == (
        "reauthorization_required"
    )


@pytest.mark.asyncio
async def test_token_endpoint_outage_stays_transient(llm, monkeypatch):
    provider, secrets, sessions = llm
    await store_expired_token(secrets)
    token_endpoint(monkeypatch, 503)

    with pytest.raises(httpx.HTTPStatusError):
        await provider._anthropic_subscription_auth(await provider_row(sessions))
    assert (await provider.get_anthropic_oauth_status("claude"))["status"] == "authorized"


@pytest.mark.asyncio
async def test_never_authorized_provider_names_the_missing_authorization(llm):
    provider, _, sessions = llm
    with pytest.raises(ProviderReauthorizationRequired) as missing:
        await provider._anthropic_subscription_auth(await provider_row(sessions))
    assert str(missing.value) == (
        "Claude Max is not authorized yet. Authorize the provider in Settings > Providers."
    )
