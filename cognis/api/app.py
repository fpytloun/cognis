"""FastAPI application factory."""

from __future__ import annotations

import asyncio
import hashlib
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request, WebSocket
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from cognis.api.common import error_response
from cognis.api.middleware import AuthenticationMiddleware
from cognis.api.routes.agents import router as agents_router
from cognis.api.routes.auth import router as auth_router
from cognis.api.routes.conversations import router as conversations_router
from cognis.api.routes.escalations import router as escalations_router
from cognis.api.routes.executors import router as executors_router
from cognis.api.routes.notifications import router as notifications_router
from cognis.api.routes.secrets import router as secrets_router
from cognis.api.routes.sessions import router as sessions_router
from cognis.api.routes.settings import router as settings_router
from cognis.api.routes.skills import router as skills_router
from cognis.api.routes.system import router as system_router
from cognis.api.routes.tasks import router as tasks_router
from cognis.api.routes.tools import router as tools_router
from cognis.api.routes.users import router as users_router
from cognis.api.routes.workflows import router as workflows_router
from cognis.api.runtime_support import build_shared_runtime, build_step_runtime_factory
from cognis.api.websocket import handle_websocket
from cognis.bootstrap import bootstrap_runtime
from cognis.config import load_config
from cognis.core.agent_loop import AgentLoop, PauseWaiter, SessionLock
from cognis.core.compaction import CompactionStrategy
from cognis.core.context import ContextAssembler
from cognis.core.decision import DecisionEngine
from cognis.core.events import EventBus
from cognis.core.remember_queue import RememberRetryQueue
from cognis.core.session import SessionManager
from cognis.core.session_cache import SessionCache
from cognis.core.step_evaluator import StepEvaluator
from cognis.core.task_queue import TaskQueue
from cognis.core.tool_output_store import ToolOutputStore
from cognis.core.tool_router import ToolRouter
from cognis.core.workflow_engine import WorkflowEngine
from cognis.core.workflow_registry import WorkflowRegistry
from cognis.logging import get_logger, setup_logging
from cognis.providers.auth.jwt import JWTAuthProvider
from cognis.providers.registry import build_provider_registry
from cognis.security import LoginRateLimiter, RequestRateLimiter, create_password_hasher
from cognis.ui_assets import SPAMiddleware, resolve_ui_build_dir


def _as_int(value: object, default: int) -> int:
    return value if isinstance(value, int) else default


def _as_user_facing_host(host: str) -> str:
    return "localhost" if host in {"0.0.0.0", "::"} else host


def _build_user_facing_url(config: object) -> str:
    return f"http://{_as_user_facing_host(config.host)}:{config.port}"  # type: ignore[attr-defined]


def _key_fingerprint(path: Path) -> str | None:
    if not path.exists():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


async def _print_startup_status(
    config: object, providers: object, ui_build_dir: Path | None
) -> None:
    base_url = _build_user_facing_url(config)
    memory_health, guardrails_health = await asyncio.gather(
        providers.memory.health(),  # type: ignore[attr-defined]
        providers.guardrails.health(),  # type: ignore[attr-defined]
    )

    if getattr(config, "serve_ui", False) and ui_build_dir is not None:
        sys.stdout.write(f"\nWeb UI: {base_url}\n")
    elif getattr(config, "serve_ui", False):
        sys.stdout.write(
            "\nWeb UI assets not found — build the UI in ui/ or set COGNIS_SERVE_UI=false.\n"
        )
    else:
        sys.stdout.write("\nWeb UI: disabled (COGNIS_SERVE_UI=false)\n")

    if memory_health.status == "healthy":
        sys.stdout.write(f"Mnemory: reachable at {config.mnemory_url}\n")  # type: ignore[attr-defined]
    else:
        sys.stdout.write(
            f"Mnemory: NOT reachable at {config.mnemory_url} — memory features will be unavailable\n"  # type: ignore[attr-defined]
        )

    if guardrails_health.status == "healthy":
        sys.stdout.write(f"Intaris: reachable at {config.intaris_url}\n")  # type: ignore[attr-defined]
    else:
        sys.stdout.write(
            f"Intaris: NOT reachable at {config.intaris_url} — guardrail features will be unavailable\n"  # type: ignore[attr-defined]
        )
    sys.stdout.flush()


logger = get_logger(__name__)


def create_app() -> FastAPI:
    config = load_config()
    setup_logging(config.log_level, config.log_format)
    ui_build_dir = resolve_ui_build_dir() if config.serve_ui else None

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
        await _print_startup_status(config_runtime, providers, ui_build_dir)

        async with session_factory() as session:
            from cognis.store.queries import count_users, ensure_default_executor, get_setting_value

            await ensure_default_executor(session)
            await session.commit()

            auth_provider.token_ttl_seconds = _as_int(
                await get_setting_value(session, "security.token_ttl_seconds", 3600), 3600
            )
            app.state.ws_auth_timeout_seconds = _as_int(
                await get_setting_value(session, "security.ws_auth_timeout_seconds", 10), 10
            )
            api_read_requests_per_minute = _as_int(
                await get_setting_value(session, "security.api_read_requests_per_minute", 600), 600
            )
            api_write_requests_per_minute = _as_int(
                await get_setting_value(session, "security.api_write_requests_per_minute", 200), 200
            )
            cache_max_entries = _as_int(
                await get_setting_value(session, "session.cache_max_entries", 200), 200
            )

            if await count_users(session) == 0:
                token = setup_token_manager.issue()
                if config_runtime.serve_ui and ui_build_dir is not None:
                    sys.stdout.write(
                        f"\nNo users found. Complete setup at:\n  {_build_user_facing_url(config_runtime)}/setup?token={token}\nThis link expires in 15 minutes.\n\n"
                    )
                else:
                    sys.stdout.write(
                        "\nNo users found. Web UI setup is unavailable, so create the first admin with:\n"
                        '  cognis admin create-user admin@example.com --name "Admin"\n\n'
                    )
                sys.stdout.flush()

        event_bus = EventBus()
        session_cache = SessionCache(providers.guardrails, max_entries=cache_max_entries)
        session_manager = SessionManager(
            session_factory, providers, session_cache, event_bus=event_bus
        )
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
        pause_waiter = PauseWaiter()
        session_lock = SessionLock()
        tool_output_store = ToolOutputStore(Path(config_runtime.data_dir))
        await tool_output_store.cleanup_expired()
        tool_router = await ToolRouter.from_session_factory(
            providers.guardrails,
            session_factory,
            memory=providers.memory,
            tool_output_store=tool_output_store,
        )
        workflow_registry = WorkflowRegistry(session_factory)
        step_evaluator = await StepEvaluator.from_session_factory(
            session_factory=session_factory,
            llm=providers.llm,
        )
        (
            shared_tool_registry,
            shared_executor_connection,
            shared_runtime_cleanup,
        ) = await build_shared_runtime(providers)
        step_runtime_factory = build_step_runtime_factory(
            providers=providers,
            shared_registry=shared_tool_registry,
            shared_connection=shared_executor_connection,
            session_factory=session_factory,
        )
        agent_loop = AgentLoop(
            providers=providers,
            session_manager=session_manager,
            session_cache=session_cache,
            context_assembler=context_assembler,
            compaction_strategy=compaction_strategy,
            tool_router=tool_router,
            remember_queue=remember_queue,
            event_bus=event_bus,
            session_lock=session_lock,
            pause_waiter=pause_waiter,
            tool_output_store=tool_output_store,
        )
        workflow_engine = WorkflowEngine(
            session_factory=session_factory,
            providers=providers,
            agent_loop=agent_loop,
            step_evaluator=step_evaluator,
            workflow_registry=workflow_registry,
            session_manager=session_manager,
            event_bus=event_bus,
            pause_waiter=pause_waiter,
            step_runtime_factory=step_runtime_factory,
            shared_tool_registry=shared_tool_registry,
            shared_executor_connection=shared_executor_connection,
            session_cache=session_cache,
        )
        task_queue = await TaskQueue.from_session_factory(
            session_factory=session_factory,
            workflow_engine=workflow_engine,
            workflow_registry=workflow_registry,
            event_bus=event_bus,
            llm_provider=providers.llm,
        )
        agent_loop.set_task_queue(task_queue)
        # Unified notification service — created early so recovery code
        # can use it.  Must be before recover_paused_tasks().
        from cognis.core.notifications import NotificationService

        notification_service = NotificationService(
            session_factory=session_factory,
            pause_waiter=pause_waiter,
            event_bus=event_bus,
            providers=providers,
        )
        agent_loop.notification_service = notification_service
        workflow_engine._notification_service = notification_service  # noqa: SLF001

        # Reconcile pending notifications from before restart (re-registers
        # PauseWaiters from DB so gates/escalations/step-questions survive).
        await notification_service.reconcile_pending()

        # TurnScheduler — core-layer turn orchestration, no WebSocket dependency.
        # Must be registered BEFORE task_queue.start() so recovered tasks
        # that complete during startup have a handler for their follow-up.
        from cognis.core.turn_scheduler import TurnScheduler

        turn_scheduler = TurnScheduler(
            session_factory=session_factory,
            workflow_engine=workflow_engine,
            decision_engine=decision_engine,
            task_queue=task_queue,
            session_manager=session_manager,
            session_cache=session_cache,
            compaction_strategy=compaction_strategy,
            agent_loop=agent_loop,
            pause_waiter=pause_waiter,
            notification_service=notification_service,
            providers=providers,
            workflow_registry=workflow_registry,
            event_bus=event_bus,
        )

        # CommandDispatcher — transport-agnostic slash command handling.
        from cognis.core.commands import CommandDispatcher

        command_dispatcher = CommandDispatcher(
            session_factory=session_factory,
            session_manager=session_manager,
            session_cache=session_cache,
            compaction_strategy=compaction_strategy,
            providers=providers,
            pause_waiter=pause_waiter,
            notification_service=notification_service,
        )

        recovered_sessions = await session_manager.recover_stale_sessions()
        recovered_tasks = await task_queue.recover_stale_tasks()
        recovered_paused_tasks = await task_queue.recover_paused_tasks()
        await task_queue.start()

        app.state.config = config_runtime
        app.state.engine = engine
        app.state.session_factory = session_factory
        app.state.setup_token_manager = setup_token_manager
        app.state.password_hasher = password_hasher
        app.state.auth_provider = auth_provider
        app.state.providers = providers
        app.state.login_rate_limiter = LoginRateLimiter()
        app.state.api_rate_limiter = RequestRateLimiter(
            read_requests_per_minute=api_read_requests_per_minute,
            write_requests_per_minute=api_write_requests_per_minute,
        )
        app.state.provider_test_results = {}
        app.state.provider_test_cooldowns = {}
        app.state.remember_queue = remember_queue
        app.state.serve_ui = config_runtime.serve_ui
        app.state.ui_build_dir = str(ui_build_dir) if ui_build_dir is not None else None
        app.state.user_facing_url = _build_user_facing_url(config_runtime)
        app.state.jwt_public_key_fingerprint = _key_fingerprint(config_runtime.jwt_public_key_path)
        app.state.session_cache = session_cache
        app.state.session_manager = session_manager
        app.state.context_assembler = context_assembler
        app.state.compaction_strategy = compaction_strategy
        app.state.decision_engine = decision_engine
        app.state.event_bus = event_bus
        app.state.pause_waiter = pause_waiter
        app.state.session_lock = session_lock
        app.state.tool_router = tool_router
        app.state.workflow_registry = workflow_registry
        app.state.step_evaluator = step_evaluator
        app.state.agent_loop = agent_loop
        app.state.workflow_engine = workflow_engine
        app.state.task_queue = task_queue
        app.state.tool_registry = shared_tool_registry
        app.state.executor_connection = shared_executor_connection
        # Store as frozensets for O(1) lookup; these are written once at
        # startup and never grow.
        app.state.recovered_session_ids = frozenset(recovered_sessions)
        app.state.recovered_task_ids = frozenset(recovered_tasks)
        app.state.recovered_paused_task_ids = frozenset(recovered_paused_tasks)

        app.state.notification_service = notification_service
        app.state.turn_scheduler = turn_scheduler
        app.state.command_dispatcher = command_dispatcher

        yield

        await task_queue.stop()
        await shared_runtime_cleanup()
        await remember_queue.stop()
        await providers.executor.cleanup()
        await providers.memory.client.aclose()
        await providers.guardrails.client.aclose()
        await engine.dispose()

    app = FastAPI(title="Cognis", version="0.1.0", lifespan=lifespan)

    # Middleware stack (execution order is bottom-to-top):
    # 1. SPA middleware — serves UI static files for non-API paths
    # 2. Auth middleware — authenticates /api/* routes
    # 3. CORS middleware — handles CORS preflight and headers
    if config.serve_ui and ui_build_dir is not None:
        app.add_middleware(SPAMiddleware, directory=ui_build_dir)
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
    app.include_router(conversations_router)
    app.include_router(agents_router)
    app.include_router(sessions_router)
    app.include_router(settings_router)
    app.include_router(tasks_router)
    app.include_router(workflows_router)
    app.include_router(secrets_router)
    app.include_router(tools_router)
    app.include_router(skills_router)
    app.include_router(executors_router)
    app.include_router(escalations_router)
    app.include_router(notifications_router)
    app.include_router(users_router)

    @app.exception_handler(StarletteHTTPException)
    async def http_exception_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        if isinstance(exc.detail, dict) and exc.detail.get("code"):
            return error_response(
                exc.status_code,
                str(exc.detail.get("code")),
                str(exc.detail.get("message", "Request failed")),
                details=exc.detail.get("details"),
            )
        return error_response(exc.status_code, "request_error", str(exc.detail))

    @app.exception_handler(RequestValidationError)
    async def request_validation_exception_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        return error_response(
            422,
            "validation_error",
            "Request validation failed",
            details={"errors": exc.errors()},
        )

    @app.exception_handler(ValueError)
    async def value_error_handler(request: Request, exc: ValueError) -> JSONResponse:
        return error_response(400, "validation_error", str(exc))

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
        logger.exception("Unhandled API exception")
        return error_response(500, "internal_error", "Internal server error")

    @app.websocket("/api/ws")
    async def websocket_endpoint(websocket: WebSocket) -> None:
        await handle_websocket(websocket)

    # NOTE: SPA serving moved to SPAMiddleware (added above) which runs
    # before the FastAPI router, avoiding the 404 exception handler
    # intercepting requests meant for the UI.

    return app
