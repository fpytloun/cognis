"""FastAPI application factory."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import secrets
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request, WebSocket
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from cognis.api.chat_v2.routes import router as chat_v2_router
from cognis.api.common import error_response
from cognis.api.middleware import AuthenticationMiddleware
from cognis.api.routes.agents import router as agents_router
from cognis.api.routes.artifacts import router as artifacts_router
from cognis.api.routes.auth import router as auth_router
from cognis.api.routes.channels import router as channels_router
from cognis.api.routes.conversations import router as conversations_router
from cognis.api.routes.credentials import router as credentials_router
from cognis.api.routes.escalations import router as escalations_router
from cognis.api.routes.executors import router as executors_router
from cognis.api.routes.images import router as images_router
from cognis.api.routes.knowledgebases import router as knowledgebases_router
from cognis.api.routes.mcp_oauth import (
    router as mcp_oauth_router,
)
from cognis.api.routes.mcp_oauth import (
    schedule_mcp_executor_reconfigure_for_app,
)
from cognis.api.routes.notifications import router as notifications_router
from cognis.api.routes.projects import router as projects_router
from cognis.api.routes.push import router as push_router
from cognis.api.routes.schedules import router as schedules_router
from cognis.api.routes.search import router as search_router
from cognis.api.routes.secrets import router as secrets_router
from cognis.api.routes.sessions import router as sessions_router
from cognis.api.routes.settings import router as settings_router
from cognis.api.routes.skills import router as skills_router
from cognis.api.routes.stt import router as stt_router
from cognis.api.routes.system import router as system_router
from cognis.api.routes.tasks import router as tasks_router
from cognis.api.routes.tools import router as tools_router
from cognis.api.routes.tts import router as tts_router
from cognis.api.routes.users import router as users_router
from cognis.api.routes.workflows import router as workflows_router
from cognis.api.runtime_support import build_shared_runtime, build_step_runtime_factory
from cognis.api.websocket import handle_websocket
from cognis.bootstrap import bootstrap_runtime
from cognis.config import load_config
from cognis.core.agent_loop import AgentLoop, PauseWaiter, SessionLock
from cognis.core.agent_registry import AgentRegistry
from cognis.core.compaction import CompactionStrategy
from cognis.core.context import ContextAssembler
from cognis.core.decision import DecisionEngine
from cognis.core.events import EventBus
from cognis.core.mcp_oauth import MCPOAuthError, MCPOAuthService
from cognis.core.remember_queue import RememberRetryQueue
from cognis.core.scheduler import Scheduler
from cognis.core.session import SessionManager
from cognis.core.session_cache import SessionCache
from cognis.core.step_evaluator import StepEvaluator
from cognis.core.step_profiles import StepProfileRegistry
from cognis.core.task_queue import TaskQueue
from cognis.core.tool_classification_queue import ToolClassificationQueue
from cognis.core.tool_output_store import ToolOutputStore
from cognis.core.tool_router import ToolRouter
from cognis.core.workflow_engine import WorkflowEngine
from cognis.core.workflow_registry import WorkflowRegistry
from cognis.logging import get_logger, setup_logging
from cognis.models.config import ProviderHealth
from cognis.providers.auth.jwt import JWTAuthProvider
from cognis.providers.registry import ProviderRegistry, build_provider_registry
from cognis.security import LoginRateLimiter, RequestRateLimiter, create_password_hasher
from cognis.ui_assets import SPAMiddleware, resolve_ui_build_dir

STARTUP_HEALTH_ATTEMPTS = 3
STARTUP_HEALTH_RETRY_DELAY_SECONDS = 0.5


def _as_int(value: object, default: int) -> int:
    return value if isinstance(value, int) else default


def _as_user_facing_host(host: str) -> str:
    return "localhost" if host in {"0.0.0.0", "::"} else host


def _build_user_facing_url(config: object) -> str:
    explicit = getattr(config, "public_base_url", "")
    if isinstance(explicit, str) and explicit:
        return explicit.rstrip("/")
    return f"http://{_as_user_facing_host(config.host)}:{config.port}"  # type: ignore[attr-defined]


def _ensure_artifact_signing_secret(config: object) -> str:
    key_path = Path(config.data_dir) / "artifact-signing.key"  # type: ignore[attr-defined]
    key_path.parent.mkdir(parents=True, exist_ok=True)
    if not key_path.exists():
        key_path.write_text(secrets.token_urlsafe(32), encoding="utf-8")
    return key_path.read_text(encoding="utf-8").strip()


def _key_fingerprint(path: Path) -> str | None:
    if not path.exists():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


async def _check_startup_provider_health(
    provider: object,
    *,
    attempts: int = STARTUP_HEALTH_ATTEMPTS,
    retry_delay_seconds: float = STARTUP_HEALTH_RETRY_DELAY_SECONDS,
) -> ProviderHealth:
    last_health = ProviderHealth(
        name=provider.__class__.__name__,
        status="degraded",
        error="health check was not executed",
    )
    bounded_attempts = max(1, attempts)
    for attempt in range(1, bounded_attempts + 1):
        try:
            last_health = await provider.health()  # type: ignore[attr-defined]
            if not isinstance(last_health, ProviderHealth):
                last_health = ProviderHealth(
                    name=provider.__class__.__name__,
                    status="degraded",
                    error="health check returned an invalid response",
                )
        except Exception as exc:
            last_health = ProviderHealth(
                name=provider.__class__.__name__,
                status="degraded",
                error=str(exc),
            )
        if last_health.status == "healthy":
            return last_health
        if attempt < bounded_attempts:
            await asyncio.sleep(retry_delay_seconds)
    return last_health


def _provider_health_note(health: ProviderHealth) -> str:
    details = health.details
    if isinstance(details, dict):
        status_code = details.get("status_code")
        if status_code is not None:
            body = details.get("body")
            return f" (HTTP {status_code}: {body})" if body else f" (HTTP {status_code})"
    if health.error:
        return f" ({health.error})"
    return ""


async def _print_startup_status(
    config: object, providers: ProviderRegistry, ui_build_dir: Path | None
) -> None:
    base_url = _build_user_facing_url(config)
    memory_health, guardrails_health = await asyncio.gather(
        _check_startup_provider_health(providers.memory),
        _check_startup_provider_health(providers.guardrails),
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
            f"Mnemory: NOT reachable at {config.mnemory_url}{_provider_health_note(memory_health)} — memory features will be unavailable\n"  # type: ignore[attr-defined]
        )

    if guardrails_health.status == "healthy":
        sys.stdout.write(f"Intaris: reachable at {config.intaris_url}\n")  # type: ignore[attr-defined]
    else:
        sys.stdout.write(
            f"Intaris: NOT reachable at {config.intaris_url}{_provider_health_note(guardrails_health)} — guardrail features will be unavailable\n"  # type: ignore[attr-defined]
        )
    sys.stdout.flush()


logger = get_logger(__name__)


def create_app() -> FastAPI:
    config = load_config()
    setup_logging(config.log_level, config.log_format)
    ui_build_dir = resolve_ui_build_dir() if config.serve_ui else None

    # Install LiteLLM ChatGPT Responses patches at process startup.
    # These are idempotent and safe to call unconditionally:
    # - cache passthrough: re-inserts prompt_cache_key/prompt_cache_retention
    #   after the upstream whitelist filter strips them.
    # - suppress default instructions: patches LiteLLM's helper so it stops
    #   prepending the ~5 KB Codex CLI prompt block to every request,
    #   eliminating duplicate instructions and hosted-drift warnings. Operators
    #   can override by setting CHATGPT_DEFAULT_INSTRUCTIONS before startup.
    from cognis.providers.llm.chatgpt_patches import (
        install_chatgpt_responses_cache_passthrough,
        suppress_chatgpt_default_instructions,
    )

    install_chatgpt_responses_cache_passthrough()
    suppress_chatgpt_default_instructions()

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
        event_bus = EventBus()
        remember_queue = RememberRetryQueue(
            providers.memory,
            session_factory=session_factory,
            event_reader=providers.guardrails,
            event_bus=event_bus,
        )
        await remember_queue.start()
        tool_classification_queue = ToolClassificationQueue(
            session_factory=session_factory,
            llm_provider=providers.llm,
        )
        await tool_classification_queue.start()
        await _print_startup_status(config_runtime, providers, ui_build_dir)

        async with session_factory() as session:
            from cognis.store.queries import count_users, ensure_default_executor, get_setting_value

            allow_in_process = bool(
                await get_setting_value(session, "executors.allow_in_process", True)
            )
            if allow_in_process:
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

        pause_waiter = PauseWaiter()
        session_lock = SessionLock()
        session_lock_sweeper_task: asyncio.Task[None] | None = None
        session_cache = SessionCache(
            providers.guardrails,
            max_entries=cache_max_entries,
            redis_url=config_runtime.redis_url,
        )
        session_manager = SessionManager(
            session_factory,
            providers,
            session_cache,
            event_bus=event_bus,
            session_lock=session_lock,
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
        providers.compaction_strategy = compaction_strategy  # type: ignore[attr-defined]
        providers.executor.in_process.compaction_strategy = compaction_strategy
        decision_engine = await DecisionEngine.from_session_factory(
            session_factory=session_factory,
            llm=providers.llm,
        )
        from cognis.core.tool_output_spool import ToolOutputSpool
        from cognis.core.tool_output_store import (
            FilesystemToolOutputBackend,
            S3ToolOutputBackend,
        )

        if config_runtime.tool_output_backend == "s3":
            tool_output_backend = S3ToolOutputBackend(
                endpoint=config_runtime.tool_output_s3_endpoint,
                access_key=config_runtime.tool_output_s3_access_key,
                secret_key=config_runtime.tool_output_s3_secret_key,
                bucket=config_runtime.tool_output_s3_bucket,
                region=config_runtime.tool_output_s3_region,
            )
        else:
            tool_output_backend = FilesystemToolOutputBackend(Path(config_runtime.data_dir))

        tool_output_store = ToolOutputStore(
            tool_output_backend,
            ttl_hours=config_runtime.tool_output_ttl_hours,
            max_size_mb=config_runtime.tool_output_max_size_mb,
        )
        tool_output_spool = ToolOutputSpool()
        await tool_output_store.cleanup_expired()

        # Artifact store for images and other binary content
        from cognis.artifacts.store import ArtifactStore, ArtifactStoreConfig

        artifact_signing_secret = (
            config_runtime.artifact_signing_secret
            or _ensure_artifact_signing_secret(config_runtime)
        )
        artifact_store = ArtifactStore(
            ArtifactStoreConfig(
                backend=config_runtime.artifact_backend,
                path=str(config_runtime.artifact_path),
                s3_endpoint=config_runtime.artifact_s3_endpoint,
                s3_access_key=config_runtime.artifact_s3_access_key,
                s3_secret_key=config_runtime.artifact_s3_secret_key,
                s3_bucket=config_runtime.artifact_s3_bucket,
                s3_region=config_runtime.artifact_s3_region,
                max_size_bytes=config_runtime.artifact_max_size_bytes,
                base_url=_build_user_facing_url(config_runtime),
                signing_secret=artifact_signing_secret,
                signed_url_ttl_seconds=config_runtime.artifact_signed_url_ttl_seconds,
            )
        )
        context_assembler.set_artifact_store(artifact_store)

        from cognis.core.artifact_maintenance import ArtifactMaintenanceService
        from cognis.store.queries import get_model_routing, get_setting_value

        artifact_maintenance = ArtifactMaintenanceService(
            session_factory=session_factory,
            artifact_store=artifact_store,
        )
        await artifact_maintenance.start()

        from cognis.knowledgebase.indexer import KnowledgebaseIndexer
        from cognis.knowledgebase.service import KnowledgebaseService
        from cognis.knowledgebase.vector import DisabledVectorBackend, QdrantVectorBackend

        kb_notes: list[str] = []
        if config_runtime.knowledgebase_vector_backend == "qdrant":
            kb_vector_backend = QdrantVectorBackend(
                url=config_runtime.knowledgebase_qdrant_url,
                api_key=config_runtime.knowledgebase_qdrant_api_key,
                collection=config_runtime.knowledgebase_qdrant_collection,
            )
        else:
            kb_vector_backend = DisabledVectorBackend()
            kb_notes.append("vector backend disabled")
        async with session_factory() as kb_session:
            kb_embedding_route = await get_model_routing(kb_session, "embedding")
        if kb_embedding_route is None:
            kb_notes.append("embedding route not configured")
        kb_backend_health = (
            await kb_vector_backend.health()
            if config_runtime.knowledgebase_vector_backend == "qdrant"
            else {"ok": False}
        )
        if config_runtime.knowledgebase_vector_backend == "qdrant" and not kb_backend_health.get(
            "ok", False
        ):
            kb_notes.append(
                f"vector backend unhealthy: {kb_backend_health.get('reason', 'unknown')}"
            )
        knowledgebase_backend_enabled = (
            config_runtime.knowledgebase_vector_backend == "qdrant"
            and bool(kb_backend_health.get("ok", False))
        )
        knowledgebase_enabled = knowledgebase_backend_enabled and kb_embedding_route is not None
        knowledgebase_service = KnowledgebaseService(
            session_factory=session_factory,
            artifact_store=artifact_store,
            llm=providers.llm,
            vector_backend=kb_vector_backend,
            enabled=knowledgebase_backend_enabled,
            disabled_notes=kb_notes,
        )
        knowledgebase_indexer = KnowledgebaseIndexer(
            session_factory=session_factory,
            artifact_store=artifact_store,
            llm=providers.llm,
            vector_backend=kb_vector_backend,
            enabled=knowledgebase_enabled,
            poll_interval_seconds=config_runtime.knowledgebase_index_poll_interval_seconds,
            max_artifact_size_bytes=config_runtime.knowledgebase_max_artifact_size_bytes,
            max_chunks_per_artifact=config_runtime.knowledgebase_max_chunks_per_artifact,
            chunk_target_tokens=config_runtime.knowledgebase_chunk_target_tokens,
            chunk_overlap_tokens=config_runtime.knowledgebase_chunk_overlap_tokens,
            embedding_batch_size=config_runtime.knowledgebase_embedding_batch_size,
        )
        await knowledgebase_indexer.start()

        tool_router = await ToolRouter.from_session_factory(
            providers.guardrails,
            session_factory,
            llm=providers.llm,
            memory=providers.memory,
            credentials_provider=providers.credentials,
            tool_output_store=tool_output_store,
            image_generation_provider=providers.image_generation,
            artifact_store=artifact_store,
            event_bus=event_bus,
        )
        agent_registry = AgentRegistry(session_factory)
        workflow_registry = WorkflowRegistry(session_factory)
        step_profile_registry = await StepProfileRegistry.from_session_factory(session_factory)
        step_evaluator = await StepEvaluator.from_session_factory(
            session_factory=session_factory,
            llm=providers.llm,
        )
        shared_runtime = await build_shared_runtime(
            providers, knowledgebase_enabled=knowledgebase_backend_enabled
        )
        step_runtime_factory = build_step_runtime_factory(
            providers=providers,
            shared_registry=shared_runtime.tool_registry,
            shared_connection=shared_runtime.executor_connection,
            session_factory=session_factory,
            artifact_store=artifact_store,
            knowledgebase_service=knowledgebase_service if knowledgebase_backend_enabled else None,
        )
        async with session_factory() as session:
            step_timeout_seconds = await get_setting_value(
                session,
                "session.step_timeout_seconds",
                3600,
            )
            llm_stream_idle_timeout_seconds = await get_setting_value(
                session,
                "session.llm_stream_idle_timeout_seconds",
                300,
            )
            llm_stream_max_retries = await get_setting_value(
                session,
                "session.llm_stream_max_retries",
                3,
            )
            anthropic_cache_ttl = await get_setting_value(
                session,
                "session.anthropic_cache_ttl",
                "5m",
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
            session_factory=session_factory,
            tool_classification_queue=tool_classification_queue,
            step_profile_registry=step_profile_registry,
            default_step_timeout_seconds=(
                int(step_timeout_seconds) if isinstance(step_timeout_seconds, int) else 3600
            ),
            default_llm_stream_idle_timeout_seconds=(
                int(llm_stream_idle_timeout_seconds)
                if isinstance(llm_stream_idle_timeout_seconds, int)
                else 300
            ),
            default_llm_stream_max_retries=(
                int(llm_stream_max_retries) if isinstance(llm_stream_max_retries, int) else 3
            ),
            default_anthropic_cache_ttl=(
                str(anthropic_cache_ttl) if anthropic_cache_ttl is not None else "5m"
            ),
            tool_output_store=tool_output_store,
            step_runtime_factory=step_runtime_factory,
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
            shared_tool_registry=shared_runtime.tool_registry,
            shared_executor_connection=shared_runtime.executor_connection,
            session_cache=session_cache,
        )
        task_queue = await TaskQueue.from_session_factory(
            session_factory=session_factory,
            workflow_engine=workflow_engine,
            workflow_registry=workflow_registry,
            event_bus=event_bus,
            agent_registry=agent_registry,
            llm_provider=providers.llm,
        )
        tool_router._task_queue = task_queue
        agent_loop.set_task_queue(task_queue)
        # Unified notification service — created early so recovery code
        # can use it.  Must be before recover_paused_tasks().
        from cognis.core.notifications import NotificationService
        from cognis.core.web_push import WebPushService, load_web_push_config

        notification_service = NotificationService(
            session_factory=session_factory,
            pause_waiter=pause_waiter,
            event_bus=event_bus,
            providers=providers,
        )

        async def _on_mcp_oauth_completed(transaction_id: str) -> None:
            await schedule_mcp_executor_reconfigure_for_app(app, transaction_id=transaction_id)

        mcp_oauth_service = MCPOAuthService(
            session_factory=session_factory,
            key_path=str(config_runtime.secrets_key_path),
            public_base_url=config_runtime.public_base_url,
            notification_service=notification_service,
            on_authorization_completed=_on_mcp_oauth_completed,
            executor_provider=providers.executor.websocket
            if hasattr(providers.executor, "websocket")
            else None,
        )
        providers.mcp_oauth_service = mcp_oauth_service  # type: ignore[attr-defined]
        tool_router._mcp_oauth_service = mcp_oauth_service  # noqa: SLF001
        if hasattr(providers.executor, "websocket"):

            async def _on_mcp_oauth_loopback_callback(
                executor_id: str,
                payload: dict[str, Any],
            ) -> None:
                try:
                    await mcp_oauth_service.complete_loopback_callback(
                        executor_id=executor_id,
                        listener_id=str(payload.get("listener_id") or ""),
                        redirect_uri=str(payload.get("redirect_uri") or ""),
                        state=str(payload.get("state") or ""),
                        code=str(payload.get("code") or "") or None,
                        error=str(payload.get("error") or "") or None,
                        error_description=str(payload.get("error_description") or "") or None,
                    )
                except MCPOAuthError:
                    logger.warning(
                        "mcp oauth: executor loopback callback failed",
                        extra={"extra_data": {"executor_id": executor_id}},
                        exc_info=True,
                    )
                except Exception:
                    logger.exception(
                        "mcp oauth: unexpected executor loopback callback failure",
                        extra={"extra_data": {"executor_id": executor_id}},
                    )

            providers.executor.websocket.register_oauth_loopback_callback(
                _on_mcp_oauth_loopback_callback
            )
        tool_router.notification_service = notification_service
        tool_router.pause_waiter = pause_waiter
        agent_loop.notification_service = notification_service
        workflow_engine._notification_service = notification_service  # noqa: SLF001

        web_push_service = WebPushService(
            session_factory=session_factory,
            event_bus=event_bus,
            config=load_web_push_config(config_runtime),
            artifact_store=artifact_store,
        )

        # Reconcile pending notifications from before restart (re-registers
        # PauseWaiters from DB so gates/escalations/step-questions survive).
        await notification_service.reconcile_pending()
        await mcp_oauth_service.recover_pending_device_authorizations()

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
            artifact_store=artifact_store,
            workflow_registry=workflow_registry,
            event_bus=event_bus,
            tool_output_spool=tool_output_spool,
        )
        agent_loop.set_turn_scheduler(turn_scheduler)

        from cognis.core.managed_conversation_maintenance import (
            ManagedConversationMaintenanceService,
        )

        managed_conversation_maintenance = ManagedConversationMaintenanceService(
            session_factory=session_factory,
            turn_scheduler=turn_scheduler,
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
            turn_scheduler=turn_scheduler,
        )

        from cognis.store.queries import get_setting_value

        async with session_factory() as session:
            session_stale_after_seconds = _as_int(
                await get_setting_value(session, "session.stale_after_seconds", 300),
                300,
            )

        recovered_sessions = await session_manager.recover_stale_sessions(
            stale_after_seconds=session_stale_after_seconds
        )
        recovered_tasks = await task_queue.recover_stale_tasks()
        recovered_paused_tasks = await task_queue.recover_paused_tasks()
        recovered_orphaned_step_runs = await task_queue.recover_orphaned_running_step_runs()

        # System-wide invariant reconciliation. Runs after the focused
        # recovery helpers above so any residual drift (e.g. conversations
        # whose active_session_id still points at a terminal session) is
        # cleaned before the drain loop starts picking tasks. The
        # implementation is idempotent and covered by unit tests.
        from cognis.core.invariants import reconcile_invariants

        async with session_factory() as recon_session:
            invariant_reports = await reconcile_invariants(recon_session)
        reconciled_invariant_counts = {
            report.category: report.reconciled_count
            for report in invariant_reports
            if report.reconciled_count
        }
        if reconciled_invariant_counts:
            logger.warning(
                "startup: reconciled invariant violations",
                extra={"extra_data": {"counts": reconciled_invariant_counts}},
            )

        logger.info(
            "startup: recovery summary",
            extra={
                "extra_data": {
                    "recovered_sessions": len(recovered_sessions),
                    "recovered_tasks": len(recovered_tasks),
                    "recovered_paused_tasks": len(recovered_paused_tasks),
                    "recovered_orphaned_step_runs": recovered_orphaned_step_runs,
                    "invariant_reports": [report.as_dict() for report in invariant_reports],
                }
            },
        )

        await task_queue.start()

        # Scheduler — evaluates cron/interval/one-shot schedules and
        # creates Tasks via task_queue.submit() when they become due.
        scheduler = Scheduler(
            session_factory=session_factory,
            task_queue=task_queue,
            event_bus=event_bus,
        )
        await scheduler.start()
        tool_router._scheduler = scheduler
        await managed_conversation_maintenance.start()

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
        app.state.tool_classification_queue = tool_classification_queue
        app.state.artifact_store = artifact_store
        app.state.chat_v2_cursor_secret = f"chat-v2:{artifact_signing_secret}"
        app.state.artifact_maintenance = artifact_maintenance
        app.state.managed_conversation_maintenance = managed_conversation_maintenance
        app.state.knowledgebase_enabled = knowledgebase_backend_enabled
        app.state.knowledgebase_service = knowledgebase_service
        app.state.knowledgebase_indexer = knowledgebase_indexer
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
        app.state.agent_registry = agent_registry
        app.state.workflow_registry = workflow_registry
        app.state.step_profile_registry = step_profile_registry
        app.state.step_evaluator = step_evaluator
        app.state.agent_loop = agent_loop
        app.state.workflow_engine = workflow_engine
        app.state.task_queue = task_queue
        app.state.scheduler = scheduler
        app.state.tool_registry = shared_runtime.tool_registry
        app.state.executor_connection = shared_runtime.executor_connection
        # Store as frozensets for O(1) lookup; these are written once at
        # startup and never grow.
        app.state.recovered_session_ids = frozenset(recovered_sessions)
        app.state.recovered_task_ids = frozenset(recovered_tasks)
        app.state.recovered_paused_task_ids = frozenset(recovered_paused_tasks)
        app.state.recovered_orphaned_step_runs = recovered_orphaned_step_runs
        app.state.startup_invariant_reports = [report.as_dict() for report in invariant_reports]

        app.state.notification_service = notification_service
        app.state.mcp_oauth_service = mcp_oauth_service
        app.state.web_push_service = web_push_service
        app.state.turn_scheduler = turn_scheduler
        app.state.tool_output_store = tool_output_store
        app.state.tool_output_spool = tool_output_spool
        app.state.command_dispatcher = command_dispatcher

        # Channel manager — lifecycle orchestration for channel adapters.
        from cognis.channels.delivery import ChannelDeliveryService
        from cognis.channels.inbound import InboundPipeline
        from cognis.channels.manager import ChannelManager
        from cognis.channels.pairing import PairingService

        # Use a lazy ref to avoid circular dependency
        _channel_manager_holder: list[ChannelManager | None] = [None]

        def _get_channel_manager() -> ChannelManager | None:
            return _channel_manager_holder[0]

        pairing_service = PairingService(
            session_factory=session_factory,
            channel_manager_ref=_get_channel_manager,
        )

        inbound_pipeline = InboundPipeline(
            session_factory=session_factory,
            turn_scheduler=turn_scheduler,
            llm_provider=providers.llm,
            session_manager=session_manager,
            pairing_service=pairing_service,
            channel_manager_ref=_get_channel_manager,
            command_dispatcher=command_dispatcher,
            notification_service=notification_service,
            credentials_provider=providers.credentials,
        )
        channel_manager = ChannelManager(
            session_factory=session_factory,
            inbound_pipeline=inbound_pipeline,
            secrets_provider=providers.secrets,
            artifact_store=artifact_store,
            event_bus=event_bus,
            ws_provider=providers.executor.websocket
            if hasattr(providers.executor, "websocket")
            else None,
        )
        _channel_manager_holder[0] = channel_manager

        channel_delivery = ChannelDeliveryService(
            session_factory=session_factory,
            event_bus=event_bus,
            channel_manager_ref=_get_channel_manager,
            turn_scheduler=turn_scheduler,
            public_base_url=config_runtime.public_base_url,
        )
        workflow_engine._channel_delivery = channel_delivery  # noqa: SLF001

        app.state.channel_manager = channel_manager
        app.state.channel_delivery = channel_delivery
        app.state.pairing_service = pairing_service

        # Start channel adapters (non-blocking — failures are logged)
        try:
            await channel_manager.start_all()
        except Exception:
            logger.exception("Failed to start channel adapters")

        try:
            await channel_delivery.recover_pending_deliveries()
        except Exception:
            logger.exception("Failed to recover pending channel deliveries")

        await channel_delivery.start()

        async def _session_lock_sweeper() -> None:
            interval_seconds = 900.0
            idle_seconds = 900.0
            while True:
                await asyncio.sleep(interval_seconds)
                for session_id in session_lock.stale_unlocked_session_ids(
                    max_idle_seconds=idle_seconds
                ):
                    session_lock.evict(session_id, reason="sweeper")

        session_lock_sweeper_task = asyncio.create_task(_session_lock_sweeper())

        yield

        if session_lock_sweeper_task is not None:
            session_lock_sweeper_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await session_lock_sweeper_task
        await managed_conversation_maintenance.stop()
        await knowledgebase_indexer.stop()
        await artifact_maintenance.stop()
        await channel_delivery.stop()
        await mcp_oauth_service.shutdown()
        await channel_manager.stop_all()
        await scheduler.stop()
        await task_queue.stop()
        await shared_runtime.cleanup()
        await remember_queue.stop()
        await tool_classification_queue.stop()
        await providers.executor.cleanup()
        await session_cache.aclose()
        await providers.memory.client.aclose()
        await providers.guardrails.client.aclose()
        await engine.dispose()

    app = FastAPI(title="Cognis", version="0.11.0", lifespan=lifespan)

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
    app.include_router(artifacts_router)
    app.include_router(channels_router)
    app.include_router(chat_v2_router)
    app.include_router(conversations_router)
    app.include_router(credentials_router)
    app.include_router(agents_router)
    app.include_router(images_router)
    app.include_router(knowledgebases_router)
    app.include_router(sessions_router)
    app.include_router(settings_router)
    app.include_router(tasks_router)
    app.include_router(schedules_router)
    app.include_router(workflows_router)
    app.include_router(secrets_router)
    app.include_router(search_router)
    app.include_router(tools_router)
    app.include_router(mcp_oauth_router)
    app.include_router(skills_router)
    app.include_router(executors_router)
    app.include_router(escalations_router)
    app.include_router(notifications_router)
    app.include_router(projects_router)
    app.include_router(push_router)
    app.include_router(tts_router)
    app.include_router(stt_router)
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

    @app.websocket("/api/executor/ws")
    async def executor_websocket_endpoint(websocket: WebSocket) -> None:
        from cognis.api.executor_ws import handle_executor_websocket

        ws_provider = app.state.providers.executor.websocket
        await handle_executor_websocket(
            websocket,
            ws_provider,
            app.state.providers,
            app.state.session_factory,
        )

    # NOTE: SPA serving moved to SPAMiddleware (added above) which runs
    # before the FastAPI router, avoiding the 404 exception handler
    # intercepting requests meant for the UI.

    return app
