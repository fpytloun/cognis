"""The production factory and real accounting sink, without an application shim."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import event, select
from sqlalchemy.exc import OperationalError

from cognis.core.usage.service import UsageAccountingService
from cognis.models.usage import UsageAttemptFinalized
from cognis.models.usage_pricing import UsagePricingPin, usage_pricing_fingerprint
from cognis.ownership import SYSTEM_USER_EMAIL
from cognis.providers.auth.jwt import JWTAuthProvider
from cognis.providers.llm.usage_capture import UsageCapture
from cognis.providers.registry import build_provider_registry
from cognis.store.database import create_engine, create_session_factory
from cognis.store.models import Base, LLMProvider
from cognis.store.usage.models import UsageAttemptRow
from tests.unit.test_provider_registry import _test_config
from tests.unit.test_usage_capture_runtime import Sink


class ProviderCalls(list[str]):
    """Content-free request-path and wire-tier evidence from the loopback peer."""

    def __init__(self) -> None:
        super().__init__()
        self.tiers: list[object] = []


@pytest_asyncio.fixture
async def capture_provider() -> AsyncIterator[tuple[str, list[str]]]:
    """A real loopback HTTP peer works with either installed SDK HTTP backend."""
    calls = ProviderCalls()
    body = json.dumps(
        {
            "id": "factory-http-response",
            "object": "response",
            "created_at": 0,
            "status": "completed",
            "error": None,
            "incomplete_details": None,
            "model": "gpt-4o-mini",
            "output": [
                {
                    "id": "factory-http-message",
                    "type": "message",
                    "status": "completed",
                    "role": "assistant",
                    "content": [
                        {"type": "output_text", "text": "PRIVATE OUTPUT", "annotations": []}
                    ],
                }
            ],
            "usage": {
                "input_tokens": 2,
                "input_tokens_details": {"cached_tokens": 0},
                "output_tokens": 1,
                "output_tokens_details": {"reasoning_tokens": 0},
                "total_tokens": 3,
            },
        }
    ).encode()

    async def upstream(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            header = await reader.readuntil(b"\r\n\r\n")
            lines = header.split(b"\r\n")
            calls.append(lines[0].decode().split(" ")[1])
            length = next(
                int(line.split(b":", 1)[1])
                for line in lines
                if line.lower().startswith(b"content-length:")
            )
            sent = json.loads(await reader.readexactly(length))
            calls.tiers.append(sent.get("service_tier", "omitted"))
            assert sent["input"][0]["content"] == "PRIVATE PROMPT"
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                b"X-Request-Id: factory-http-request\r\nConnection: close\r\n"
                + f"Content-Length: {len(body)}\r\n\r\n".encode()
                + body
            )
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(upstream, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        yield f"http://127.0.0.1:{port}/v1", calls
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_disabled_capture_preserves_actual_factory_inference(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capture_provider
) -> None:
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path}/disabled.db")
    sessions = create_session_factory(engine)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    config = _test_config(tmp_path, str(engine.url))
    auth = JWTAuthProvider(config.jwt_private_key_path, config.jwt_public_key_path)
    registry = build_provider_registry(config, sessions, auth, usage_capture=None)
    monkeypatch.setenv("COGNIS_FACTORY_TEST_API_KEY", "factory-test-key")
    async with sessions() as session:
        session.add(
            LLMProvider(
                provider_id="factory-capture",
                display_name="Factory capture",
                owner_email=SYSTEM_USER_EMAIL,
                location="controller",
                backend="litellm",
                config={
                    "preset": "openai",
                    "default_model": "gpt-4o-mini",
                    "base_url": capture_provider[0],
                    "api_base": capture_provider[0],
                    "auth_config": {"mode": "env", "env_var": "COGNIS_FACTORY_TEST_API_KEY"},
                },
                status="active",
            )
        )
        await session.commit()
    try:
        assert registry.llm.usage_capture is None
        response = await registry.llm.generate(
            messages=[{"role": "user", "content": "PRIVATE PROMPT"}],
            model="gpt-4o-mini",
            provider_id="factory-capture",
            acting_user_email="factory-owner@example.com",
            max_retries=0,
        )
        assert response["choices"][0]["message"]["content"] == "PRIVATE OUTPUT"
        assert capture_provider[1] == ["/v1/responses"]
        async with sessions() as session:
            assert not (await session.scalars(select(UsageAttemptRow))).all()
    finally:
        await registry.llm.aclose()
        await engine.dispose()


@pytest.mark.asyncio
async def test_real_factory_requires_explicit_capture(tmp_path: Path) -> None:
    url = f"sqlite+aiosqlite:///{tmp_path}/factory.db"
    engine = create_engine(url)
    config = _test_config(tmp_path, url)
    auth = JWTAuthProvider(config.jwt_private_key_path, config.jwt_public_key_path)
    try:
        with pytest.raises(TypeError, match="usage_capture"):
            build_provider_registry(config, create_session_factory(engine), auth)
    finally:
        await engine.dispose()


@pytest.mark.parametrize("tier", ["omitted", "auto", "priority"])
async def test_real_factory_wire_tier_controls_exact_price_lookup(
    tmp_path,
    monkeypatch,
    capture_provider,
    tier,
):
    """Actual factory/adapter dispatch, not metadata-default applicability proof."""
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path}/pricing-factory.db")
    sessions = create_session_factory(engine)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    config = _test_config(tmp_path, str(engine.url))
    auth = JWTAuthProvider(config.jwt_private_key_path, config.jwt_public_key_path)
    provider_config = {
        "preset": "openai",
        "default_model": "gpt-4o-mini",
        "base_url": capture_provider[0],
        "api_base": capture_provider[0],
        "auth_config": {"mode": "env", "env_var": "COGNIS_FACTORY_TEST_API_KEY"},
    }
    pin = UsagePricingPin("upr_factory_priority", "ups_factory_priority")
    expected = {}
    lookups = []

    def lookup(*, provider_id, model, metadata_fingerprint):
        key = provider_id, model, metadata_fingerprint
        lookups.append(key)
        return expected.get(key)

    sink = Sink()
    capture = UsageCapture(
        sink=sink,
        controller_owner_id="factory-pricing",
        coverage_epoch_id="factory-epoch",
        price_lookup=lookup,
    )
    registry = build_provider_registry(config, sessions, auth, usage_capture=capture)
    monkeypatch.setenv("COGNIS_FACTORY_TEST_API_KEY", "factory-test-key")
    try:
        async with sessions() as session:
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
            await session.commit()
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
        expected[("factory-pricing", "gpt-4o-mini", fingerprint)] = pin
        result = await registry.llm.generate(
            messages=[{"role": "user", "content": "PRIVATE PROMPT"}],
            model="gpt-4o-mini",
            provider_id="factory-pricing",
            acting_user_email="factory-owner@example.com",
            max_retries=0,
            **({} if tier == "omitted" else {"service_tier": tier}),
        )
        assert result["choices"][0]["message"]["content"] == "PRIVATE OUTPUT"
        assert capture_provider[1].tiers == [tier]
        finals = [event for event in sink.offers if isinstance(event, UsageAttemptFinalized)]
        assert len(finals) == 1
        selection = finals[0].context.selection
        if tier == "priority":
            assert selection.service_tier == "priority"
            assert selection.effective_parameters.service_tier == selection.service_tier
            assert lookups == list(expected)
            assert selection.captured_price_revision_id == pin.captured_price_revision_id
            assert selection.resolver_snapshot_id == pin.resolver_snapshot_id
        elif tier == "omitted":
            # A request naming no tier is billed at the model's default tier.
            standard = usage_pricing_fingerprint(
                provider_id="factory-pricing",
                model="gpt-4o-mini",
                model_info=info,
                provider_config=provider_config,
                service_tier="standard",
            )
            assert selection.service_tier == "standard"
            assert lookups == [("factory-pricing", "gpt-4o-mini", standard)]
            assert selection.captured_price_revision_id is None
        else:
            assert selection.service_tier is None
            assert not lookups
            assert selection.captured_price_revision_id is None
            assert selection.resolver_snapshot_id is None
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("accepting", "database_failure"),
    [(True, False), (False, False), (True, True)],
    ids=["writer-running", "writer-not-started", "writer-database-failure"],
)
async def test_factory_capture_reaches_real_sdk_and_writer_without_gating_inference(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    accepting: bool,
    database_failure: bool,
    capture_provider: tuple[str, list[str]],
) -> None:
    url = f"sqlite+aiosqlite:///{tmp_path}/factory.db"
    engine = create_engine(url)
    sessions = create_session_factory(engine)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    config = _test_config(tmp_path, url)
    auth = JWTAuthProvider(config.jwt_private_key_path, config.jwt_public_key_path)
    wakes: list[None] = []
    accounting = UsageAccountingService(
        sessions,
        controller_owner_id=config.controller_id,
        projection_waker=lambda: wakes.append(None),
    )
    capture = UsageCapture(
        sink=accounting,
        controller_owner_id=accounting.controller_owner_id,
        coverage_epoch_id=accounting.coverage_epoch_id,
    )
    registry = build_provider_registry(config, sessions, auth, usage_capture=capture)
    assert registry.llm.usage_capture is capture
    assert registry.image_generation is registry.llm
    monkeypatch.setenv("COGNIS_FACTORY_TEST_API_KEY", "factory-test-key")
    async with sessions() as session:
        session.add(
            LLMProvider(
                provider_id="factory-capture",
                display_name="Factory capture",
                owner_email=SYSTEM_USER_EMAIL,
                location="controller",
                backend="litellm",
                config={
                    "preset": "openai",
                    "default_model": "gpt-4o-mini",
                    "base_url": capture_provider[0],
                    "api_base": capture_provider[0],
                    "auth_config": {"mode": "env", "env_var": "COGNIS_FACTORY_TEST_API_KEY"},
                },
                status="active",
            )
        )
        await session.commit()
    if accepting:
        await accounting.start()

    def unavailable(connection, cursor, statement, parameters, context, executemany):
        if statement.lower().lstrip().startswith("insert into usage_attempts"):
            raise OperationalError("PRIVATE DRIVER TEXT", {}, RuntimeError("PRIVATE ERROR"))

    if database_failure:
        event.listen(engine.sync_engine, "before_cursor_execute", unavailable)
    try:
        result = await registry.llm.generate(
            messages=[{"role": "user", "content": "PRIVATE PROMPT"}],
            model="gpt-4o-mini",
            provider_id="factory-capture",
            acting_user_email="factory-owner@example.com",
            max_retries=0,
        )
        assert result["choices"][0]["message"]["content"] == "PRIVATE OUTPUT"
        assert capture_provider[1] == ["/v1/responses"]
        await accounting.drain()
        if database_failure:
            event.remove(engine.sync_engine, "before_cursor_execute", unavailable)
        async with sessions() as session:
            attempts = (await session.scalars(select(UsageAttemptRow))).all()
        if accepting and not database_failure:
            assert len(attempts) == 1
            attempt = attempts[0]
            assert attempt.principal_subject == "factory-owner@example.com"
            assert attempt.initiating_actor == "factory-owner@example.com"
            assert attempt.provider_payer == SYSTEM_USER_EMAIL
            assert attempt.session_id is None
            assert attempt.outcome == "success"
            assert attempt.input_tokens == 2 and attempt.output_tokens == 1
            assert attempt.controller_owner_id == accounting.controller_owner_id
            assert attempt.coverage_epoch_id == accounting.coverage_epoch_id
            encoded = json.dumps([attempt.context_payload, attempt.terminal_payload])
            assert "PRIVATE PROMPT" not in encoded and "PRIVATE OUTPUT" not in encoded
            assert wakes
        else:
            assert not attempts
            if not accepting:
                assert capture.rejected_events >= 2
            else:
                assert capture.rejected_events == 0
            assert accounting.diagnostics().dropped_finalized == 1
    finally:
        await accounting.aclose()
        await engine.dispose()
