"""FastAPI application factory."""

from __future__ import annotations

import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket
from fastapi.middleware.cors import CORSMiddleware

from cognis.api.middleware import AuthenticationMiddleware
from cognis.api.routes.auth import router as auth_router
from cognis.api.routes.system import router as system_router
from cognis.api.websocket import handle_websocket
from cognis.bootstrap import bootstrap_runtime
from cognis.config import load_config
from cognis.core.compaction import CompactionStrategy
from cognis.core.context import ContextAssembler
from cognis.core.decision import DecisionEngine
from cognis.core.remember_queue import RememberRetryQueue
from cognis.core.session import SessionManager
from cognis.core.session_cache import SessionCache
from cognis.logging import setup_logging
from cognis.providers.auth.jwt import JWTAuthProvider
from cognis.providers.registry import build_provider_registry
from cognis.security import LoginRateLimiter, create_password_hasher


def _as_int(value: object, default: int) -> int:
    return value if isinstance(value, int) else default


def create_app() -> FastAPI:
    config = load_config()
    setup_logging(config.log_level, config.log_format)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        password_hasher = create_password_hasher()
        config_runtime, engine, session_factory, setup_token_manager = await bootstrap_runtime(
            config, password_hasher
        )
        auth_provider = JWTAuthProvider(
            config_runtime.jwt_private_key_path, config_runtime.jwt_public_key_path
        )
        providers = build_provider_registry(config_runtime, session_factory, auth_provider)
        remember_queue = RememberRetryQueue(providers.memory)
        await remember_queue.start()

        async with session_factory() as session:
            from cognis.store.queries import count_users, get_setting_value

            auth_provider.token_ttl_seconds = _as_int(
                await get_setting_value(session, "security.token_ttl_seconds", 3600), 3600
            )
            app.state.ws_auth_timeout_seconds = _as_int(
                await get_setting_value(session, "security.ws_auth_timeout_seconds", 10), 10
            )
            cache_max_entries = _as_int(
                await get_setting_value(session, "session.cache_max_entries", 200), 200
            )

            if await count_users(session) == 0:
                token = setup_token_manager.issue()
                sys.stdout.write(
                    f"\nNo users found. Complete setup at:\n  http://{config_runtime.host}:{config_runtime.port}/setup?token={token}\nThis link expires in 15 minutes.\n\n"
                )
                sys.stdout.flush()

        session_cache = SessionCache(providers.guardrails, max_entries=cache_max_entries)
        session_manager = SessionManager(session_factory, providers, session_cache)
        context_assembler = await ContextAssembler.from_session_factory(
            session_factory=session_factory,
            memory=providers.memory,
            guardrails=providers.guardrails,
            llm=providers.llm,
            session_cache=session_cache,
            session_manager=session_manager,
        )
        compaction_strategy = await CompactionStrategy.from_session_factory(
            session_factory=session_factory,
            guardrails=providers.guardrails,
            llm=providers.llm,
            session_cache=session_cache,
        )
        decision_engine = await DecisionEngine.from_session_factory(
            session_factory=session_factory,
            llm=providers.llm,
        )
        await session_manager.recover_stale_sessions()

        app.state.config = config_runtime
        app.state.engine = engine
        app.state.session_factory = session_factory
        app.state.setup_token_manager = setup_token_manager
        app.state.password_hasher = password_hasher
        app.state.auth_provider = auth_provider
        app.state.providers = providers
        app.state.login_rate_limiter = LoginRateLimiter()
        app.state.remember_queue = remember_queue
        app.state.session_cache = session_cache
        app.state.session_manager = session_manager
        app.state.context_assembler = context_assembler
        app.state.compaction_strategy = compaction_strategy
        app.state.decision_engine = decision_engine

        yield

        await remember_queue.stop()
        await providers.executor.cleanup()
        await providers.memory.client.aclose()
        await providers.guardrails.client.aclose()
        await engine.dispose()

    app = FastAPI(title="Cognis", version="0.1.0", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=config.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.add_middleware(AuthenticationMiddleware)
    app.include_router(auth_router)
    app.include_router(system_router)

    @app.websocket("/api/ws")
    async def websocket_endpoint(websocket: WebSocket) -> None:
        await handle_websocket(websocket)

    return app
