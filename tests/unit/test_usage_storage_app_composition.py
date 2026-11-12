"""Real app/factory startup order and failure cleanup, without external inference."""

import asyncio
import hmac
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from starlette.types import Receive, Scope, Send

import cognis.api.app as app_module
from cognis.api.usage_lifecycle import UsageDeliveryLifecycleMiddleware
from cognis.config import load_config
from cognis.mfa import MfaSecretCipher
from cognis.models.config import ModelInfo
from cognis.store.models import Setting


@pytest.mark.asyncio
async def test_disabled_analytics_composes_no_runtime_capture_or_query_routes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = replace(
        load_config(),
        data_dir=tmp_path,
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'app.db'}",
        jwt_private_key_path=tmp_path / "keys" / "private.pem",
        jwt_public_key_path=tmp_path / "keys" / "public.pem",
        secrets_key_path=tmp_path / "secrets.key",
        vapid_private_key_path=tmp_path / "keys" / "vapid.pem",
        artifact_path=tmp_path / "artifacts",
        redis_url="",
        serve_ui=False,
        require_external_crypto=False,
        runtime_mode="simple",
    )
    monkeypatch.setattr(app_module, "load_config", lambda: config)
    real_bootstrap = app_module.bootstrap_runtime
    real_factory = app_module.build_provider_registry
    constructed = {}

    class CompositionObserved(RuntimeError):
        pass

    async def bootstrap(*args, **kwargs):
        result = await real_bootstrap(*args, **kwargs)
        constructed["engine"] = result[1]
        from cognis.store.database import create_session_factory

        async with create_session_factory(result[1])() as session:
            setting = await session.get(Setting, "usage.enabled")
            assert setting is not None
            setting.value = False
            await session.commit()
        return result

    def factory(*args, usage_capture, **kwargs):
        assert usage_capture is None
        assert application.state.usage_analytics_enabled is False
        assert application.state.usage_runtime is None
        assert application.state.usage_quota_recorder is None
        providers = real_factory(*args, usage_capture=usage_capture, **kwargs)
        constructed["providers"] = providers
        assert providers.llm.usage_capture is None

        async def inspect_composition(**_kwargs):
            assert application.state.usage_query_service is None
            assert application.state.usage_price_publisher is None
            assert not any(route.path.startswith("/api/v1/usage/") for route in application.routes)
            raise CompositionObserved

        monkeypatch.setattr(providers.executor.websocket, "configure_cluster", inspect_composition)
        return providers

    monkeypatch.setattr(app_module, "bootstrap_runtime", bootstrap)
    monkeypatch.setattr(app_module, "build_provider_registry", factory)
    application = app_module.create_app()
    try:
        with pytest.raises(CompositionObserved):
            async with application.router.lifespan_context(application):
                pytest.fail("The composition checkpoint must interrupt startup")
    finally:
        if "providers" in constructed:
            await constructed["providers"].llm.aclose()
        if "engine" in constructed:
            await constructed["engine"].dispose()


@pytest.mark.asyncio
async def test_unstarted_health_request_does_not_require_capture() -> None:
    messages = []

    async def health(scope: Scope, receive: Receive, send: Send) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"healthy"})

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        messages.append(message)

    middleware = UsageDeliveryLifecycleMiddleware(health, capture=lambda: None)
    await middleware({"type": "http"}, receive, send)
    assert messages[-1]["body"] == b"healthy"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failpoint", ["provider_factory", "query_composed", "closing_before_failure"]
)
async def test_actual_app_starts_writer_before_factory_and_closes_on_startup_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failpoint: str,
) -> None:
    config = replace(
        load_config(),
        data_dir=tmp_path,
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'app.db'}",
        jwt_private_key_path=tmp_path / "keys" / "private.pem",
        jwt_public_key_path=tmp_path / "keys" / "public.pem",
        secrets_key_path=tmp_path / "secrets.key",
        vapid_private_key_path=tmp_path / "keys" / "vapid.pem",
        artifact_path=tmp_path / "artifacts",
        redis_url="",
        serve_ui=False,
        require_external_crypto=False,
        runtime_mode="simple",
    )
    monkeypatch.setattr(app_module, "load_config", lambda: config)
    real_factory = app_module.build_provider_registry
    real_bootstrap = app_module.bootstrap_runtime
    constructed = {}

    class StopAfterFactory(RuntimeError):
        pass

    async def bootstrap(*args, **kwargs):
        result = await real_bootstrap(*args, **kwargs)
        constructed["engine"] = result[1]
        return result

    def factory(*args, usage_capture, **kwargs):
        runtime = application.state.usage_runtime
        assert usage_capture is runtime.capture
        assert not runtime.service.diagnostics().writer_failed
        assert runtime.projector.diagnostics.running
        providers = real_factory(*args, usage_capture=usage_capture, **kwargs)
        constructed["providers"] = providers
        assert providers.llm.usage_capture is usage_capture
        if failpoint == "provider_factory":
            raise StopAfterFactory("bounded test startup interruption")
        providers.llm.get_model_info = AsyncMock(return_value=ModelInfo(model_id="unknown"))

        async def stop_after_query(**_kwargs):
            if failpoint == "closing_before_failure":
                entered, release = asyncio.Event(), asyncio.Event()
                original_close = runtime.service.aclose

                async def blocked_close():
                    entered.set()
                    await release.wait()
                    await original_close()

                monkeypatch.setattr(runtime.service, "aclose", blocked_close)
                owner = asyncio.create_task(runtime.aclose())
                try:
                    await asyncio.wait_for(entered.wait(), 5)
                    owner.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await owner
                    assert runtime.projector.diagnostics.running
                finally:

                    async def release_after_owner_yields():
                        release.set()

                    constructed["release_task"] = asyncio.create_task(release_after_owner_yields())
            raise StopAfterFactory("bounded test composition interruption")

        monkeypatch.setattr(providers.executor.websocket, "configure_cluster", stop_after_query)
        return providers

    monkeypatch.setattr(app_module, "bootstrap_runtime", bootstrap)
    monkeypatch.setattr(app_module, "build_provider_registry", factory)
    application = app_module.create_app()
    try:
        with pytest.raises(StopAfterFactory):
            async with application.router.lifespan_context(application):
                pytest.fail("The deliberate startup interruption must propagate")
        runtime = application.state.usage_runtime
        assert not runtime.projector.diagnostics.running
        assert not runtime.service.diagnostics().writer_failed
        assert not runtime.diagnostics.shutdown_failed
        assert runtime.capture is constructed["providers"].llm.usage_capture
        if failpoint != "provider_factory":
            from cognis.core.usage.query_cursors import UsageCursors
            from cognis.core.usage.query_service import UsageQueryService

            assert isinstance(application.state.usage_query_service, UsageQueryService)
            assert application.state.usage_price_publisher is runtime.price_publisher
            assert not runtime.price_publisher.diagnostics.running
            assert any(route.path == "/api/v1/usage/query" for route in application.routes)
            # A separate controller using the same configured secrets key accepts
            # these cursors; process-local randomness and raw-key reuse cannot pass.
            peer = UsageCursors(
                hmac.digest(
                    MfaSecretCipher(config.secrets_key_path).key_material,
                    b"cognis.usage.cursors.v1",
                    "sha256",
                )
            )
            cursor = application.state.usage_query_service.cursors.encode("authorized_query", [1])
            assert peer.decode(cursor, "authorized_query") == [1]
    finally:
        if "release_task" in constructed:
            await constructed["release_task"]
            await application.state.usage_runtime.aclose()
        if "providers" in constructed:
            await constructed["providers"].llm.aclose()
        if "engine" in constructed:
            await constructed["engine"].dispose()
