"""Conversation and session lifecycle management."""

from __future__ import annotations

import copy
import hashlib
import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from cognis.core.events import Event, EventBus, EventType
from cognis.core.followups import FollowUpPolicy
from cognis.core.immutable_prefix import (
    PREFIX_EVENT_TYPES,
    ImmutablePrefixEntry,
    build_context_snapshot_event,
    build_prefix_message_events,
)
from cognis.core.session_cache import CachedEvent
from cognis.core.session_event_types import INTARIS_APPENDABLE_EVENT_TYPES
from cognis.core.session_fork import fork_session_events
from cognis.logging import get_logger
from cognis.models.agent import AgentDefinition
from cognis.models.session import (
    ConversationContext,
    ConversationModel,
    SessionEvent,
    SessionModel,
    SessionStatus,
    with_session_events_turn_id,
)
from cognis.runtime_context import (
    current_effective_working_directory,
    current_executor_environment,
    current_workspace_root,
    scoped_runtime_context,
)
from cognis.store import queries

logger = get_logger(__name__)

_WHITESPACE_RE = re.compile(r"\s+")
_MAX_INTENTION_LENGTH = 500
_MAX_STATUS_REASON_LENGTH = 500
_USABLE_REDO_SESSION_STATUSES = {SessionStatus.ACTIVE, SessionStatus.IDLE}


def _hash_log_value(value: str | None) -> str | None:
    if not value:
        return None
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


@dataclass(slots=True)
class HistoryRebaseResult:
    """Result of same-conversation history undo/redo."""

    operation: str
    session: SessionModel
    previous_session: SessionModel
    undo_available: bool
    redo_available: bool
    message: str


# Cognis → Intaris session status mapping.  Intaris does not have
# ``failed`` or ``cancelled``; both map to ``terminated`` with a
# machine-readable ``status_reason`` prefix.
_INTARIS_STATUS_MAP: dict[str, str] = {
    "active": "active",
    "idle": "idle",
    "completed": "completed",
    "suspended": "suspended",
    "terminated": "terminated",
    "failed": "terminated",
    "cancelled": "terminated",
}


def _map_cognis_to_intaris_status(
    cognis_status: str,
    *,
    completion_reason: str | None = None,
    result_summary: str | None = None,
    reason: str | None = None,
) -> tuple[str, str | None]:
    """Map a Cognis session status to an Intaris status + status_reason.

    Returns ``(intaris_status, status_reason)``.  The ``status_reason``
    is always bounded to ``_MAX_STATUS_REASON_LENGTH`` characters and
    uses machine-readable prefixes — never raw user/model content.
    """

    intaris_status = _INTARIS_STATUS_MAP.get(cognis_status, cognis_status)

    status_reason: str | None = None
    if cognis_status in ("failed", "cancelled"):
        status_reason = f"source_status={cognis_status}"
    elif cognis_status == "completed" and completion_reason:
        status_reason = f"completion_reason={completion_reason}"
    elif reason:
        status_reason = reason

    if status_reason and len(status_reason) > _MAX_STATUS_REASON_LENGTH:
        status_reason = status_reason[:_MAX_STATUS_REASON_LENGTH]

    return intaris_status, status_reason


def _normalize_intention(value: str | None, fallback: str = "Conversation") -> str:
    """Normalize a session intention to Intaris-safe length and whitespace."""

    collapsed = _WHITESPACE_RE.sub(" ", (value or "").strip())
    resolved = collapsed or fallback
    if len(resolved) <= _MAX_INTENTION_LENGTH:
        return resolved
    return resolved[: _MAX_INTENTION_LENGTH - 3].rstrip() + "..."


def _resolve_runtime_workdir() -> str | None:
    """Pick the most specific working directory from the runtime context vars."""

    workdir = current_effective_working_directory.get() or current_workspace_root.get()
    if isinstance(workdir, str) and workdir.strip():
        return workdir
    environment = current_executor_environment.get()
    cwd = getattr(environment, "cwd", None)
    if isinstance(cwd, str) and cwd.strip():
        return cwd
    home = getattr(environment, "home", None)
    if isinstance(home, str) and home.strip():
        return home
    return None


def executor_home_from_workspace_root(workspace_root: str | None) -> str | None:
    """Best-effort executor home inference from a known executor workspace path."""

    if isinstance(workspace_root, str):
        marker = "/src/"
        marker_index = workspace_root.find(marker)
        if marker_index > 0:
            return workspace_root[:marker_index]
    return None


def _resolve_executor_home() -> str | None:
    """Return the active executor home directory from runtime context, if known."""

    environment = current_executor_environment.get()
    home = getattr(environment, "home", None)
    if isinstance(home, str) and home.strip():
        return home
    return executor_home_from_workspace_root(current_workspace_root.get())


def _expand_executor_user_path(value: str, executor_home: str | None = None) -> str:
    if not executor_home or not (value == "~" or value.startswith("~/")):
        return value
    return f"{executor_home.rstrip('/')}{value[1:]}"


def _normalize_executor_path(value: str | None, *, executor_home: str | None = None) -> str | None:
    """Return an executor-visible absolute path for Intaris session policy."""

    if not isinstance(value, str) or not value.strip():
        return None
    stripped = _expand_executor_user_path(value.strip(), executor_home=executor_home)
    try:
        expanded = os.path.expandvars(stripped)
        if expanded == "~" or expanded.startswith("~/"):
            return expanded
        return os.path.realpath(expanded)
    except OSError:
        return stripped


async def _project_source_paths(db_session: AsyncSession, project_id: str | None) -> list[str]:
    """Return local_path entries from a project's sources, if any."""

    if not project_id:
        return []
    try:
        sources = await queries.list_project_sources(db_session, project_id)
    except Exception:
        # Best-effort: missing/unreadable project sources should not block
        # session creation. Intaris path policy enforcement still works
        # without project sources, just less broadly.
        return []
    paths: list[str] = []
    seen: set[str] = set()
    for source in sources:
        local_path = (source.local_path or "").strip()
        if not local_path or local_path in seen:
            continue
        paths.append(local_path)
        seen.add(local_path)
    return paths


def _intaris_session_details(
    working_directory: str | None,
    *,
    source: str = "cognis",
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the ``details`` payload for ``guardrails.create_session``.

    Including ``working_directory`` enables Intaris's classifier path
    policy: in-project reads fast-path through the read-only allowlist
    instead of falling through to the LLM evaluation path.
    """

    details: dict[str, Any] = {"source": source}
    if working_directory:
        details["working_directory"] = working_directory
    if extra:
        for key, value in extra.items():
            if value is not None:
                details[key] = value
    return details


def _derive_intaris_source_label(
    working_directory: str | None,
    *,
    project_paths: list[str],
    executor_home: str | None = None,
    fallback: str = "cognis",
) -> str:
    """Use the matching project source basename for Intaris display details."""

    executor_home = executor_home or _resolve_executor_home()
    normalized_workdir = _normalize_executor_path(working_directory, executor_home=executor_home)
    if not normalized_workdir:
        return fallback
    for project_path in project_paths:
        normalized_project_path = _normalize_executor_path(
            project_path, executor_home=executor_home
        )
        if normalized_project_path and (
            normalized_workdir == normalized_project_path
            or normalized_workdir.startswith(f"{normalized_project_path}/")
        ):
            return normalized_project_path.rstrip("/").rsplit("/", 1)[-1] or fallback
    return fallback


def _intaris_session_policy(
    working_directory: str | None,
    *,
    project_paths: list[str] | None = None,
    executor_home: str | None = None,
    executor_tmpdir: str | None = None,
) -> dict[str, Any]:
    """Build the ``policy`` payload for ``guardrails.create_session``.

    ``allow_paths`` widens the in-project boundary so that read tools targeting
    the executor-visible working directory, common scratch directories, or any
    configured project source remain on Intaris's fast path. Do not add
    controller-local paths here; remote executors have a different filesystem
    namespace.
    """

    executor_home = executor_home or _resolve_executor_home()
    paths: list[str] = []
    seen: set[str] = set()

    def _add(raw_path: str | None) -> None:
        path = _normalize_executor_path(raw_path, executor_home=executor_home)
        if not path:
            return
        pattern = f"{path.rstrip('/')}/*"
        if pattern not in seen:
            paths.append(pattern)
            seen.add(pattern)

    _add("/tmp")
    _add("/var/tmp")
    _add(executor_tmpdir)
    if executor_home:
        _add(executor_home)
        _add(f"{executor_home.rstrip('/')}/.local/share/cognis")
    _add(working_directory)
    for raw_path in project_paths or []:
        _add(raw_path)

    return {"allow_paths": paths} if paths else {}


def _intaris_session_context(
    working_directory: str | None,
    *,
    project_paths: list[str],
) -> tuple[dict[str, Any], str]:
    executor_home = _resolve_executor_home()
    policy = _intaris_session_policy(
        working_directory,
        project_paths=project_paths,
        executor_home=executor_home,
        executor_tmpdir=getattr(current_executor_environment.get(), "tmpdir", None),
    )
    source_label = _derive_intaris_source_label(
        working_directory,
        project_paths=project_paths,
        executor_home=executor_home,
    )
    return policy, source_label


class SessionManager:
    """Manage conversation/session metadata and external session correlation."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        providers: Any,
        session_cache: Any,
        event_bus: EventBus | None = None,
        session_lock: Any = None,
    ) -> None:
        self.session_factory = session_factory
        self.providers = providers
        self.session_cache = session_cache
        self.event_bus = event_bus
        self.session_lock = session_lock

    async def _evict_session_state(self, session_id: str) -> None:
        await self.session_cache.evict(session_id)
        if self.session_lock is not None:
            self.session_lock.evict(session_id)

    async def refresh_intaris_session_policy(self, session: SessionModel) -> None:
        """Widen an existing Intaris session policy when runtime paths become known."""

        workdir = _resolve_runtime_workdir()
        if not workdir:
            return
        async with self.session_factory() as db_session:
            project_id = await self._lookup_conversation_project_id(
                db_session, session.conversation_id
            )
            project_paths = await _project_source_paths(db_session, project_id)
        new_policy = _intaris_session_policy(workdir, project_paths=project_paths)
        if not new_policy.get("allow_paths"):
            return
        intaris_session_id = session.intaris_session_id or session.session_id
        if hasattr(self.providers.guardrails, "update_session_policy"):
            try:
                await self.providers.guardrails.update_session_policy(
                    intaris_session_id,
                    agent_id=session.agent_id,
                    user_id=session.user_email,
                    details=_intaris_session_details(workdir),
                    policy=new_policy,
                )
            except Exception:
                logger.warning(
                    "session: failed to refresh Intaris session policy",
                    extra={"extra_data": {"session_id": session.session_id}},
                    exc_info=True,
                )

    async def create_conversation(
        self,
        *,
        user_email: str,
        agent_id: str,
        context: ConversationContext,
        title: str | None = None,
        title_source: str = "unset",
        conversation_id: str | None = None,
        project_id: str | None = None,
    ) -> ConversationModel:
        """Create a conversation without creating a root session."""

        async with self.session_factory() as db_session:
            try:
                conversation = await queries.create_conversation(
                    db_session,
                    user_email=user_email,
                    agent_id=agent_id,
                    context_type=context.type,
                    title=title,
                    title_source=title_source,
                    context_ref=context.ref,
                    context_data=context.platform_data,
                    memory_labels=dict(context.memory_labels),
                    conversation_id=conversation_id,
                    project_id=project_id,
                )
                await db_session.commit()
            except Exception:
                await db_session.rollback()
                raise
        return _to_conversation_model(conversation)

    async def create_root_session(
        self,
        *,
        conversation_id: str,
        user_email: str,
        agent_id: str,
        intention: str,
        session_id: str | None = None,
    ) -> SessionModel:
        """Create a root session and corresponding Intaris session."""

        normalized_intention = _normalize_intention(intention)

        logger.info(
            "session: creating root session",
            extra={
                "extra_data": {
                    "conversation_id": conversation_id,
                    "agent_id": agent_id,
                    "user_email": user_email,
                }
            },
        )
        async with self.session_factory() as db_session:
            try:
                agent = await self._require_agent(db_session, agent_id)
                session_row = await queries.create_session(
                    db_session,
                    conversation_id=conversation_id,
                    user_email=user_email,
                    agent_id=agent_id,
                    session_id=session_id,
                )
                project_id = await self._lookup_conversation_project_id(db_session, conversation_id)
                project_paths = await _project_source_paths(db_session, project_id)
                workdir = _resolve_runtime_workdir()
                with scoped_runtime_context(
                    user_email=user_email,
                    agent_id=agent_id,
                    agent_owner_email=agent.owner_email,
                ):
                    await self.providers.guardrails.create_session(
                        session_id=session_row.session_id,
                        intention=normalized_intention,
                        agent_id=agent_id,
                        user_id=user_email,
                        details=_intaris_session_details(workdir),
                        policy=_intaris_session_policy(workdir, project_paths=project_paths),
                    )
                await queries.set_session_intaris_session_id(
                    db_session, session_row.session_id, session_row.session_id
                )
                await queries.update_conversation_active_session(
                    db_session, conversation_id, session_row.session_id
                )
                await db_session.commit()
            except Exception:
                await db_session.rollback()
                raise
        session_row.intaris_session_id = session_row.session_id
        logger.info(
            "session: root session created",
            extra={
                "extra_data": {
                    "session_id": session_row.session_id,
                    "conversation_id": conversation_id,
                    "agent_id": agent_id,
                }
            },
        )
        return _to_session_model(session_row)

    async def ensure_root_session(
        self,
        *,
        conversation_id: str,
        user_email: str,
        agent_id: str,
        intention: str,
    ) -> SessionModel:
        """Create exactly one active root session for a conversation."""

        normalized_intention = _normalize_intention(intention)
        async with self.session_factory() as db_session:
            session_row = None
            try:
                agent = await self._require_agent(db_session, agent_id)
                session_row = await queries.create_session(
                    db_session,
                    conversation_id=conversation_id,
                    user_email=user_email,
                    agent_id=agent_id,
                )
                project_id = await self._lookup_conversation_project_id(db_session, conversation_id)
                project_paths = await _project_source_paths(db_session, project_id)
                workdir = _resolve_runtime_workdir()
                source_label = _derive_intaris_source_label(
                    workdir,
                    project_paths=project_paths,
                )
                with scoped_runtime_context(
                    user_email=user_email,
                    agent_id=agent_id,
                    agent_owner_email=agent.owner_email,
                ):
                    await self.providers.guardrails.create_session(
                        session_id=session_row.session_id,
                        intention=normalized_intention,
                        agent_id=agent_id,
                        user_id=user_email,
                        details=_intaris_session_details(workdir, source=source_label),
                        policy=_intaris_session_policy(workdir, project_paths=project_paths),
                    )
                await queries.set_session_intaris_session_id(
                    db_session, session_row.session_id, session_row.session_id
                )
                claimed = await queries.update_conversation_active_session_if_unset(
                    db_session, conversation_id, session_row.session_id
                )
                if not claimed:
                    winner = await queries.get_conversation(db_session, conversation_id)
                    active_session_id = winner.active_session_id if winner is not None else None
                    await db_session.delete(session_row)
                    await db_session.commit()
                    if active_session_id is None:
                        raise RuntimeError("Lost root-session bootstrap race without winner")
                    winner_row = await queries.get_session_row(db_session, active_session_id)
                    if winner_row is None:
                        raise RuntimeError("Active root session missing after bootstrap race")
                    return _to_session_model(winner_row)
                await db_session.commit()
            except Exception:
                await db_session.rollback()
                raise

        if session_row is None:
            raise RuntimeError("Root session bootstrap failed")
        session_row.intaris_session_id = session_row.session_id
        return _to_session_model(session_row)

    async def create_conversation_with_root_session(
        self,
        *,
        user_email: str,
        agent_id: str,
        context: ConversationContext,
        title: str | None = None,
        title_source: str = "unset",
        intention: str | None = None,
        initial_active_executor_id: str | None = None,
        initial_active_executor_assigned_at: datetime | None = None,
        initial_active_executor_expires_at: datetime | None = None,
        initial_active_executor_source: str | None = None,
        project_id: str | None = None,
    ) -> tuple[ConversationModel, SessionModel]:
        """Create a conversation and root session atomically.

        Stage 36: when ``initial_active_executor_id`` is provided, it is
        copied into the new conversation's ``active_executor_id`` so the
        conversation starts already pinned. Used by the workflow engine to
        propagate the task-level pin into each step conversation.
        """

        async with self.session_factory() as db_session:
            try:
                agent = await self._require_agent(db_session, agent_id)
                conversation = await queries.create_conversation(
                    db_session,
                    user_email=user_email,
                    agent_id=agent_id,
                    context_type=context.type,
                    title=title,
                    title_source=title_source,
                    context_ref=context.ref,
                    context_data=context.platform_data,
                    memory_labels=dict(context.memory_labels),
                    project_id=project_id,
                )
                # Stage 36: seed the conversation's active_executor_id from
                # the task-level pin (if provided). The runtime factory
                # treats the conversation pin as authoritative, so this
                # carries the agent's prior choice into the new step.
                if initial_active_executor_id:
                    conversation.active_executor_id = initial_active_executor_id
                    conversation.active_executor_assigned_at = initial_active_executor_assigned_at
                    conversation.active_executor_expires_at = initial_active_executor_expires_at
                    conversation.active_executor_source = initial_active_executor_source
                session_row = await queries.create_session(
                    db_session,
                    conversation_id=conversation.conversation_id,
                    user_email=user_email,
                    agent_id=agent_id,
                )
                resolved_intention = _normalize_intention(
                    intention or self._build_root_intention(agent, title)
                )
                conversation_project_id = getattr(conversation, "project_id", None)
                project_paths = await _project_source_paths(db_session, conversation_project_id)
                workdir = _resolve_runtime_workdir()
                source_label = _derive_intaris_source_label(
                    workdir,
                    project_paths=project_paths,
                )
                with scoped_runtime_context(
                    user_email=user_email,
                    agent_id=agent_id,
                    agent_owner_email=agent.owner_email,
                ):
                    await self.providers.guardrails.create_session(
                        session_id=session_row.session_id,
                        intention=resolved_intention,
                        agent_id=agent_id,
                        user_id=user_email,
                        details=_intaris_session_details(workdir, source=source_label),
                        policy=_intaris_session_policy(workdir, project_paths=project_paths),
                    )
                await queries.set_session_intaris_session_id(
                    db_session, session_row.session_id, session_row.session_id
                )
                await queries.update_conversation_active_session(
                    db_session, conversation.conversation_id, session_row.session_id
                )
                await db_session.commit()
            except Exception:
                await db_session.rollback()
                raise

        conversation.active_session_id = session_row.session_id
        session_row.intaris_session_id = session_row.session_id
        return _to_conversation_model(conversation), _to_session_model(session_row)

    async def fork_into_new_conversation(
        self,
        *,
        source_session: SessionModel,
        source_conversation: ConversationModel,
        agent: AgentDefinition,
        user_email: str,
        title: str | None = None,
        intention: str | None = None,
        context: ConversationContext | None = None,
        extra_prefix_entries: list[ImmutablePrefixEntry] | None = None,
        extra_history_events: list[SessionEvent] | None = None,
        snapshot_extras: dict[str, Any] | None = None,
    ) -> tuple[ConversationModel, SessionModel, bool]:
        """Fork a source session into a new web conversation."""

        fork_context = context or ConversationContext(
            type="web",
            ref=None,
            platform_data={
                "forked_from": "conversation",
                "forked_from_conversation_id": source_conversation.conversation_id,
                "forked_from_session_id": source_session.session_id,
            },
            memory_labels=dict(source_conversation.context.memory_labels),
        )
        fork_title = title or (
            f"Fork: {source_conversation.title}" if source_conversation.title else "Forked chat"
        )
        conversation, session = await self.create_conversation_with_root_session(
            user_email=user_email,
            agent_id=agent.agent_id,
            context=fork_context,
            title=fork_title,
            title_source="manual",
            intention=intention or source_session.result_summary or fork_title,
        )
        copied = await fork_session_events(
            providers=self.providers,
            session_cache=self.session_cache,
            source_cognis_session_id=source_session.session_id,
            source_intaris_session_id=source_session.intaris_session_id
            or source_session.session_id,
            target_session=session,
            source_label="conversation_fork",
            snapshot_source="fork",
            snapshot_extras={
                "forked_from_conversation_id": source_conversation.conversation_id,
                "forked_from_session_id": source_session.session_id,
                **(snapshot_extras or {}),
            },
            extra_prefix_entries=extra_prefix_entries,
            extra_history_events=extra_history_events,
        )
        return conversation, session, copied

    async def undo_last_turn(
        self,
        *,
        conversation: ConversationModel,
        current_session: SessionModel,
        is_slash_command_message: Any,
        intention: str | None = None,
    ) -> HistoryRebaseResult | None:
        """Create a same-conversation root session that excludes the last user turn."""

        events = await self._read_history_events(current_session)
        undo_event = self._find_last_real_user_event(events, is_slash_command_message)
        if undo_event is None:
            return None

        cutoff_seq, cutoff_turn_id = self._undo_cutoff(events, undo_event)
        retained_events = [
            event
            for event in events
            if event.type not in PREFIX_EVENT_TYPES and event.seq < cutoff_seq
        ]
        retained_max_seq = max((event.seq for event in retained_events), default=0)

        new_session = await self._create_history_rebase_session(
            current_session=current_session,
            intention=intention or current_session.result_summary or "Undo conversation history",
        )
        copied = await fork_session_events(
            providers=self.providers,
            session_cache=self.session_cache,
            source_cognis_session_id=current_session.session_id,
            source_intaris_session_id=current_session.intaris_session_id
            or current_session.session_id,
            target_session=new_session,
            source_label="history_undo",
            snapshot_source="undo",
            snapshot_extras={
                "operation": "undo",
                "undo_of_session_id": current_session.session_id,
                "undo_cutoff_seq": cutoff_seq,
                "undo_cutoff_turn_id": cutoff_turn_id,
            },
            max_source_seq=retained_max_seq,
            event_filter=lambda event: (
                event.type not in PREFIX_EVENT_TYPES and event.seq < cutoff_seq
            ),
            record_source="cognis:undo",
        )
        if retained_events and not copied:
            raise RuntimeError("Could not copy retained history into undo session")

        metadata = {
            "operation": "undo",
            "redo_session_id": current_session.session_id,
            "undo_session_id": new_session.session_id,
            "undo_of_session_id": current_session.session_id,
            "undo_cutoff_seq": cutoff_seq,
            "undo_cutoff_turn_id": cutoff_turn_id,
            "created_at": datetime.now(UTC).isoformat(),
        }
        async with self.session_factory() as db_session:
            try:
                await queries.update_conversation_active_session(
                    db_session, conversation.conversation_id, new_session.session_id
                )
                await queries.set_conversation_history_rebase_metadata(
                    db_session, conversation.conversation_id, metadata
                )
                await db_session.commit()
            except Exception:
                await db_session.rollback()
                raise

        self._copy_runtime_overrides(current_session.session_id, new_session.session_id)
        return HistoryRebaseResult(
            operation="undo",
            session=new_session,
            previous_session=current_session,
            undo_available=bool(retained_events),
            redo_available=True,
            message="Undid last turn.",
        )

    async def redo_last_undo(
        self,
        *,
        conversation: ConversationModel,
        current_session: SessionModel,
    ) -> HistoryRebaseResult | None:
        """Restore the saved redo session when the undo branch has not diverged."""

        metadata = dict((conversation.context.platform_data or {}).get("history_rebase") or {})
        redo_session_id = metadata.get("redo_session_id")
        undo_session_id = metadata.get("undo_session_id")
        if not isinstance(redo_session_id, str) or undo_session_id != current_session.session_id:
            await self.clear_history_redo_metadata(conversation.conversation_id)
            return None

        async with self.session_factory() as db_session:
            try:
                redo_row = await queries.get_session_row(db_session, redo_session_id)
                if (
                    redo_row is None
                    or redo_row.conversation_id != conversation.conversation_id
                    or redo_row.user_email != conversation.user_email
                    or redo_row.status not in _USABLE_REDO_SESSION_STATUSES
                ):
                    await queries.clear_conversation_history_rebase_metadata(
                        db_session, conversation.conversation_id
                    )
                    await db_session.commit()
                    return None
                await queries.update_conversation_active_session(
                    db_session, conversation.conversation_id, redo_session_id
                )
                await queries.clear_conversation_history_rebase_metadata(
                    db_session, conversation.conversation_id
                )
                await db_session.commit()
            except Exception:
                await db_session.rollback()
                raise

        return HistoryRebaseResult(
            operation="redo",
            session=_to_session_model(redo_row),
            previous_session=current_session,
            undo_available=True,
            redo_available=False,
            message="Redid last turn.",
        )

    async def clear_history_redo_metadata(self, conversation_id: str) -> None:
        """Best-effort clear of stale redo metadata."""

        async with self.session_factory() as db_session:
            try:
                await queries.clear_conversation_history_rebase_metadata(
                    db_session, conversation_id
                )
                await db_session.commit()
            except Exception:
                await db_session.rollback()
                raise

    async def _create_history_rebase_session(
        self,
        *,
        current_session: SessionModel,
        intention: str,
    ) -> SessionModel:
        async with self.session_factory() as db_session:
            try:
                agent = await self._require_agent(db_session, current_session.agent_id)
                session_row = await queries.create_session(
                    db_session,
                    conversation_id=current_session.conversation_id,
                    user_email=current_session.user_email,
                    agent_id=current_session.agent_id,
                    previous_session_id=None,
                    mnemory_session_id=None,
                )
                project_id = await self._lookup_conversation_project_id(
                    db_session, current_session.conversation_id
                )
                project_paths = await _project_source_paths(db_session, project_id)
                workdir = _resolve_runtime_workdir()
                with scoped_runtime_context(
                    user_email=current_session.user_email,
                    agent_id=current_session.agent_id,
                    agent_owner_email=agent.owner_email,
                ):
                    await self.providers.guardrails.create_session(
                        session_id=session_row.session_id,
                        intention=_normalize_intention(intention),
                        agent_id=current_session.agent_id,
                        user_id=current_session.user_email,
                        details=_intaris_session_details(workdir, source="cognis:undo"),
                        policy=_intaris_session_policy(workdir, project_paths=project_paths),
                    )
                await queries.set_session_intaris_session_id(
                    db_session, session_row.session_id, session_row.session_id
                )
                await db_session.commit()
            except Exception:
                await db_session.rollback()
                raise
        session_row.intaris_session_id = session_row.session_id
        return _to_session_model(session_row)

    async def _read_history_events(self, session: SessionModel) -> list[CachedEvent]:
        cache_entry = self.session_cache.get_entry(session.session_id)
        if cache_entry is not None and cache_entry.initialized and cache_entry.events:
            return sorted(list(cache_entry.events), key=lambda event: event.seq)
        event_read = await self.providers.guardrails.read_events(
            session_id=session.intaris_session_id or session.session_id,
            after_seq=0,
        )
        events: list[CachedEvent] = []
        for raw_event in sorted(event_read.events, key=lambda event: int(event.get("seq", 0) or 0)):
            events.append(
                CachedEvent(
                    seq=int(raw_event.get("seq", 0) or 0),
                    type=str(raw_event.get("type") or ""),
                    data=dict(raw_event.get("data") or {}),
                    source=raw_event.get("source"),
                    ts=raw_event.get("ts"),
                )
            )
        return events

    @staticmethod
    def _find_last_real_user_event(
        events: list[CachedEvent],
        is_slash_command_message: Any,
    ) -> CachedEvent | None:
        for event in reversed(events):
            if event.type != "user_message":
                continue
            content = event.data.get("content")
            if isinstance(content, str) and is_slash_command_message(content):
                continue
            return event
        return None

    @staticmethod
    def _undo_cutoff(events: list[CachedEvent], undo_event: CachedEvent) -> tuple[int, str | None]:
        raw_turn_id = undo_event.data.get("turn_id")
        turn_id = raw_turn_id if isinstance(raw_turn_id, str) and raw_turn_id else None
        if turn_id:
            turn_event_seqs = [
                event.seq for event in events if event.data.get("turn_id") == turn_id
            ]
            if turn_event_seqs:
                return min(turn_event_seqs), turn_id
        return undo_event.seq, turn_id

    def _copy_runtime_overrides(self, source_session_id: str, target_session_id: str) -> None:
        model_override = self.session_cache.get_model_override(source_session_id)
        reasoning_override = self.session_cache.get_reasoning_effort_override(source_session_id)
        if model_override is not None:
            self.session_cache.set_model_override(target_session_id, model_override)
        if reasoning_override is not None:
            self.session_cache.set_reasoning_effort_override(target_session_id, reasoning_override)

    async def create_child_session(
        self,
        parent_session: SessionModel,
        *,
        mode: str,
        task_description: str,
        agent_id: str,
        effective_agent_id: str,
        expected_output: str | None = None,
        constraints: dict[str, Any] | None = None,
        intention: str | None = None,
        workspace_root: str | None = None,
        working_directory: str | None = None,
    ) -> SessionModel:
        """Create a delegated child session and corresponding Intaris session."""

        async with self.session_factory() as db_session:
            try:
                child_agent = await self._require_agent(db_session, agent_id)
                session_row = await queries.create_session(
                    db_session,
                    conversation_id=parent_session.conversation_id,
                    user_email=parent_session.user_email,
                    agent_id=agent_id,
                    parent_session_id=parent_session.session_id,
                    delegation_mode=mode,
                    delegation_task=task_description,
                )
                resolved_intention = _normalize_intention(
                    intention or self._build_child_intention(child_agent, task_description)
                )
                project_id = await self._lookup_conversation_project_id(
                    db_session, parent_session.conversation_id
                )
                project_paths = await _project_source_paths(db_session, project_id)
                workdir = working_directory or workspace_root
                child_details = _intaris_session_details(
                    workdir,
                    extra={
                        "delegated_by_agent": parent_session.agent_id,
                        "effective_agent_id": effective_agent_id,
                        "task_description": task_description,
                        "expected_output": expected_output,
                        "constraints": constraints or {},
                    },
                )
                with scoped_runtime_context(
                    user_email=parent_session.user_email,
                    agent_id=parent_session.agent_id,
                    agent_owner_email=child_agent.owner_email,
                ):
                    await self.providers.guardrails.create_session(
                        session_id=session_row.session_id,
                        intention=resolved_intention,
                        agent_id=agent_id,
                        user_id=parent_session.user_email,
                        parent_session_id=parent_session.intaris_session_id
                        or parent_session.session_id,
                        details=child_details,
                        policy=_intaris_session_policy(workdir, project_paths=project_paths),
                    )
                await queries.set_session_intaris_session_id(
                    db_session, session_row.session_id, session_row.session_id
                )
                await db_session.commit()
            except Exception:
                await db_session.rollback()
                raise

        session_row.intaris_session_id = session_row.session_id
        return _to_session_model(session_row)

    async def attach_mnemory_session(self, session_id: str, mnemory_session_id: str) -> bool:
        """Persist the first Mnemory session ID for a Cognis session."""

        if not mnemory_session_id.strip():
            return False
        async with self.session_factory() as db_session:
            try:
                updated = await queries.set_session_mnemory_session_id(
                    db_session, session_id, mnemory_session_id
                )
                await db_session.commit()
                return updated
            except Exception:
                await db_session.rollback()
                raise

    async def _sync_intaris_status(
        self,
        session_id: str,
        cognis_status: str,
        *,
        user_email: str | None = None,
        completion_reason: str | None = None,
        result_summary: str | None = None,
        reason: str | None = None,
    ) -> None:
        """Best-effort sync of session status to Intaris.

        This is the ONLY place that calls the Intaris status-update
        provider method.  Failures are logged at warning level and
        never raised — Intaris unavailability must not block Cognis
        session transitions.
        """

        intaris_status, status_reason = _map_cognis_to_intaris_status(
            cognis_status,
            completion_reason=completion_reason,
            result_summary=result_summary,
            reason=reason,
        )

        resolved_user_email = user_email
        target_session_id = session_id
        if resolved_user_email is None:
            async with self.session_factory() as db_session:
                session_row = await queries.get_session_row(db_session, session_id)
                if session_row is not None:
                    resolved_user_email = session_row.user_email
                    target_session_id = session_row.intaris_session_id or session_row.session_id

        try:
            await self.providers.guardrails.update_session_status(
                target_session_id,
                intaris_status,
                status_reason,
                user_email=resolved_user_email,
            )
        except Exception:
            logger.warning(
                "session: failed to sync status to Intaris",
                extra={
                    "extra_data": {
                        "session_id": session_id,
                        "target_session_id": target_session_id,
                        "uses_intaris_session_id": target_session_id != session_id,
                        "cognis_status": cognis_status,
                        "intaris_status": intaris_status,
                        "has_user_email": bool(resolved_user_email),
                        "user_email_hash": _hash_log_value(resolved_user_email),
                    }
                },
                exc_info=True,
            )

    async def mark_idle(self, session_id: str) -> bool:
        """Mark a session idle and evict any warm cache entry."""

        async with self.session_factory() as db_session:
            try:
                updated = await queries.set_session_idle(db_session, session_id)
                await db_session.commit()
            except Exception:
                await db_session.rollback()
                raise
        await self._evict_session_state(session_id)
        if updated:
            await self._sync_intaris_status(session_id, "idle")
        return updated

    async def mark_active(self, session_id: str) -> bool:
        """Mark a session active again when a new turn starts."""

        async with self.session_factory() as db_session:
            try:
                updated = await queries.set_session_active(db_session, session_id)
                await db_session.commit()
            except Exception:
                await db_session.rollback()
                raise
        if updated:
            await self._sync_intaris_status(session_id, "active")
        return updated

    async def mark_completed(
        self,
        session_id: str,
        result_summary: str | None = None,
        result_content: str | None = None,
        completion_reason: str | None = None,
    ) -> bool:
        """Mark a session completed and evict cache state."""

        async with self.session_factory() as db_session:
            try:
                updated = await queries.set_session_status(
                    db_session,
                    session_id,
                    SessionStatus.COMPLETED,
                    completed_at=datetime.now(UTC),
                    result_summary=result_summary,
                    result_content=result_content,
                    completion_reason=completion_reason,
                )
                await db_session.commit()
            except Exception:
                await db_session.rollback()
                raise
        await self._evict_session_state(session_id)
        if updated:
            await self._sync_intaris_status(
                session_id,
                "completed",
                completion_reason=completion_reason,
                result_summary=result_summary,
            )
        return updated

    async def mark_failed(
        self,
        session_id: str,
        result_summary: str | None = None,
        result_content: str | None = None,
    ) -> bool:
        """Mark a session failed and evict cache state."""

        async with self.session_factory() as db_session:
            try:
                updated = await queries.set_session_status(
                    db_session,
                    session_id,
                    SessionStatus.FAILED,
                    completed_at=datetime.now(UTC),
                    result_summary=result_summary,
                    result_content=result_content,
                )
                await db_session.commit()
            except Exception:
                await db_session.rollback()
                raise
        await self._evict_session_state(session_id)
        if updated:
            await self._sync_intaris_status(session_id, "failed")
        return updated

    async def mark_cancelled(self, session_id: str, result_summary: str | None = None) -> bool:
        """Mark a session cancelled and evict cache state."""

        async with self.session_factory() as db_session:
            try:
                updated = await queries.set_session_status(
                    db_session,
                    session_id,
                    SessionStatus.CANCELLED,
                    completed_at=datetime.now(UTC),
                    result_summary=result_summary,
                )
                await db_session.commit()
            except Exception:
                await db_session.rollback()
                raise
        await self._evict_session_state(session_id)
        if updated:
            await self._sync_intaris_status(session_id, "cancelled")
        return updated

    async def mark_suspended(self, session_id: str, reason: str | None = None) -> bool:
        """Mark a session suspended (e.g., safety concern from Intaris)."""

        async with self.session_factory() as db_session:
            try:
                updated = await queries.set_session_status(
                    db_session,
                    session_id,
                    SessionStatus.SUSPENDED,
                    result_summary=reason,
                )
                await db_session.commit()
            except Exception:
                await db_session.rollback()
                raise
        if updated:
            await self._sync_intaris_status(session_id, "suspended", reason=reason)
        return updated

    async def mark_terminated(self, session_id: str, reason: str | None = None) -> bool:
        """Mark a session terminated (hard-kill by user or system)."""

        async with self.session_factory() as db_session:
            try:
                updated = await queries.set_session_status(
                    db_session,
                    session_id,
                    SessionStatus.TERMINATED,
                    completed_at=datetime.now(UTC),
                    result_summary=reason,
                )
                await db_session.commit()
            except Exception:
                await db_session.rollback()
                raise
        await self._evict_session_state(session_id)
        if updated:
            await self._sync_intaris_status(session_id, "terminated", reason=reason)
        return updated

    async def rotate_session(
        self,
        *,
        conversation_id: str,
        current_session: SessionModel,
        intention: str,
        completion_reason: str = "compacted",
        compaction_summary: str | None = None,
        tail_events: list[Any] | None = None,
    ) -> SessionModel:
        """Create a new root session, completing the current one.

        This is used for compaction (new clean context window) and for
        explicit session reset.  The new session starts with
        ``mnemory_session_id=None`` so the first recall creates a fresh
        Mnemory session and reconstructs the full immutable prefix
        (core memories + instructions) from scratch.  This intentionally
        resets Mnemory-side deduplication — the old session's
        ``known_memory_ids`` are stale after compaction anyway.
        """

        logger.info(
            "session: rotating root session",
            extra={
                "extra_data": {
                    "conversation_id": conversation_id,
                    "old_session_id": current_session.session_id,
                    "completion_reason": completion_reason,
                }
            },
        )

        get_prefix_entries = getattr(self.session_cache, "get_prefix_entries", None)
        prefix_entries = (
            get_prefix_entries(current_session.session_id) if callable(get_prefix_entries) else []
        )

        async with self.session_factory() as db_session:
            try:
                # 1. Mark current session completed
                # NOTE: result_summary is metadata only — no LLM-generated content
                # in the Cognis DB. The actual compaction summary lives in Intaris.
                await queries.set_session_status(
                    db_session,
                    current_session.session_id,
                    SessionStatus.COMPLETED,
                    completed_at=datetime.now(UTC),
                    result_summary=f"Rotated ({completion_reason})",
                    completion_reason=completion_reason,
                )

                # 2. Create new root session (fresh Mnemory session — the
                #    first recall will create a new Mnemory session and
                #    reconstruct the full immutable prefix from scratch)
                new_session_row = await queries.create_session(
                    db_session,
                    conversation_id=conversation_id,
                    user_email=current_session.user_email,
                    agent_id=current_session.agent_id,
                    previous_session_id=current_session.session_id,
                    mnemory_session_id=None,
                )

                # 3. Create Intaris session for the new root
                project_id = await self._lookup_conversation_project_id(db_session, conversation_id)
                project_paths = await _project_source_paths(db_session, project_id)
                workdir = _resolve_runtime_workdir()
                with scoped_runtime_context(
                    user_email=current_session.user_email,
                    agent_id=current_session.agent_id,
                    agent_owner_email=(
                        await self._require_agent(db_session, current_session.agent_id)
                    ).owner_email,
                ):
                    await self.providers.guardrails.create_session(
                        session_id=new_session_row.session_id,
                        intention=_normalize_intention(intention),
                        agent_id=current_session.agent_id,
                        user_id=current_session.user_email,
                        details=_intaris_session_details(workdir),
                        policy=_intaris_session_policy(workdir, project_paths=project_paths),
                    )

                await queries.set_session_intaris_session_id(
                    db_session, new_session_row.session_id, new_session_row.session_id
                )

                # 4. Update conversation root
                await queries.update_conversation_active_session(
                    db_session, conversation_id, new_session_row.session_id
                )

                await db_session.commit()
            except Exception:
                await db_session.rollback()
                raise

        # Evict old session cache
        await self._evict_session_state(current_session.session_id)

        # Sync old session status to Intaris (best-effort)
        await self._sync_intaris_status(
            current_session.intaris_session_id or current_session.session_id,
            "completed",
            completion_reason=completion_reason,
        )

        new_session_row.intaris_session_id = new_session_row.session_id
        new_session = _to_session_model(new_session_row)
        rotated_prefix: list[ImmutablePrefixEntry] = [
            entry for entry in prefix_entries if entry.source != "compaction_summary"
        ]
        if compaction_summary:
            rotated_prefix.append(
                ImmutablePrefixEntry(
                    role="developer",
                    source="compaction_summary",
                    content=compaction_summary,
                )
            )
        durable_summary_events = (
            with_session_events_turn_id(
                [
                    SessionEvent(
                        type="compaction_summary",
                        data={
                            "summary": compaction_summary,
                            "method": "rotation",
                            "trigger": completion_reason,
                            "source_session_id": current_session.session_id,
                        },
                    )
                ],
                None,
            )
            if compaction_summary
            else []
        )
        summary_result: Any | None = None
        if durable_summary_events:
            summary_result = await self.providers.guardrails.record_events(
                session_id=new_session.intaris_session_id or new_session.session_id,
                events=durable_summary_events,
                source="cognis",
                idempotency_key=f"{new_session.session_id}:compaction_summary:rotation",
            )
            if not summary_result.ok:
                raise RuntimeError("failed to persist rotated compaction summary")

        if rotated_prefix:
            message_events = with_session_events_turn_id(
                build_prefix_message_events(rotated_prefix),
                None,
            )
            append_result = await self.providers.guardrails.record_events(
                session_id=new_session.intaris_session_id or new_session.session_id,
                events=message_events,
                source="cognis",
                idempotency_key=f"{new_session.session_id}:immutable_prefix:compaction:messages",
            )
            if append_result.ok:
                resolved_entries = [
                    ImmutablePrefixEntry(
                        role=entry.role,
                        source=entry.source,
                        content=entry.content,
                        seq=append_result.first_seq + index,
                    )
                    for index, entry in enumerate(rotated_prefix)
                ]
                snapshot_event = build_context_snapshot_event(
                    resolved_entries,
                    snapshot_source="compaction",
                    extras={"parent_session_id": current_session.session_id},
                )
                snapshot_events = with_session_events_turn_id([snapshot_event], None)
                snapshot_result = await self.providers.guardrails.record_events(
                    session_id=new_session.intaris_session_id or new_session.session_id,
                    events=snapshot_events,
                    source="cognis",
                    idempotency_key=f"{new_session.session_id}:immutable_prefix:compaction:snapshot",
                )
                if snapshot_result.ok:
                    append_recorded_events = getattr(
                        self.session_cache, "append_recorded_events", None
                    )
                    if callable(append_recorded_events):
                        await append_recorded_events(new_session, message_events, append_result)
                        await append_recorded_events(new_session, snapshot_events, snapshot_result)
                    store_prefix_snapshot = getattr(
                        self.session_cache, "store_prefix_snapshot", None
                    )
                    if callable(store_prefix_snapshot):
                        await store_prefix_snapshot(
                            new_session.session_id,
                            resolved_entries,
                            snapshot_seq=snapshot_result.last_seq,
                            snapshot_source="compaction",
                        )
                else:
                    logger.warning(
                        "session: failed to persist compaction snapshot event",
                        extra={"extra_data": {"session_id": new_session.session_id}},
                    )
            else:
                logger.warning(
                    "session: failed to persist compaction prefix messages",
                    extra={"extra_data": {"session_id": new_session.session_id}},
                )
        if durable_summary_events and summary_result is not None:
            append_recorded_events = getattr(self.session_cache, "append_recorded_events", None)
            if callable(append_recorded_events):
                await append_recorded_events(new_session, durable_summary_events, summary_result)

        if tail_events:
            await self._seed_rotated_tail_events(
                new_session,
                tail_events=tail_events,
                previous_session_id=current_session.session_id,
            )

        logger.info(
            "session: rotation completed",
            extra={
                "extra_data": {
                    "conversation_id": conversation_id,
                    "old_session_id": current_session.session_id,
                    "new_session_id": new_session.session_id,
                }
            },
        )
        return new_session

    async def _seed_rotated_tail_events(
        self,
        new_session: SessionModel,
        *,
        tail_events: list[Any],
        previous_session_id: str,
    ) -> None:
        """Copy recent un-compacted events into the rotated session."""

        session_id = new_session.intaris_session_id or new_session.session_id
        cloned_events: list[SessionEvent] = []
        skipped_event_types: dict[str, int] = {}
        for event in tail_events:
            event_type = getattr(event, "type", None)
            data = getattr(event, "data", None)
            if not isinstance(event_type, str) or not isinstance(data, dict):
                continue
            if event_type not in INTARIS_APPENDABLE_EVENT_TYPES:
                skipped_event_types[event_type] = skipped_event_types.get(event_type, 0) + 1
                continue
            cloned_data = copy.deepcopy(data)
            cloned_data.setdefault("compaction_tail", True)
            cloned_data.setdefault("source_session_id", previous_session_id)
            source_seq = getattr(event, "seq", None)
            if isinstance(source_seq, int):
                cloned_data.setdefault("source_seq", source_seq)
            cloned_events.append(SessionEvent(type=event_type, data=cloned_data))
        if skipped_event_types:
            logger.info(
                "session: skipped non-appendable compaction tail events",
                extra={
                    "extra_data": {
                        "session_id": new_session.session_id,
                        "skipped_count": sum(skipped_event_types.values()),
                        "skipped_types": dict(sorted(skipped_event_types.items())),
                    }
                },
            )
        if not cloned_events:
            return
        try:
            append_result = await self.providers.guardrails.record_events(
                session_id=session_id,
                events=cloned_events,
                source="cognis",
                idempotency_key=f"{new_session.session_id}:compaction_tail:{previous_session_id}",
            )
            append_recorded_events = getattr(self.session_cache, "append_recorded_events", None)
            if append_result.ok and callable(append_recorded_events):
                await append_recorded_events(new_session, cloned_events, append_result)
        except Exception as exc:
            extra_data: dict[str, Any] = {"session_id": new_session.session_id}
            if isinstance(exc, httpx.HTTPStatusError):
                extra_data["response_status_code"] = exc.response.status_code
                extra_data["response_body"] = exc.response.text[:1000]
            logger.warning(
                "session: failed to seed compaction tail events",
                extra={"extra_data": extra_data},
                exc_info=True,
            )

    async def recover_stale_sessions(self, stale_after_seconds: int = 300) -> list[str]:
        """Mark stale active sessions idle on controller startup."""

        updated_before = datetime.now(UTC) - timedelta(seconds=stale_after_seconds)
        recovered_ids: list[str] = []
        recovered_child_sessions: list[Any] = []
        async with self.session_factory() as db_session:
            try:
                stale_sessions = await queries.list_stale_active_sessions(
                    db_session, updated_before
                )
                for stale_session in stale_sessions:
                    if stale_session.parent_session_id is not None:
                        continue
                    if stale_session.session_id in recovered_ids:
                        continue
                    await queries.set_session_idle(
                        db_session,
                        stale_session.session_id,
                        idle_since=datetime.now(UTC),
                    )
                    recovered_ids.append(stale_session.session_id)
                    child_ids = await self._fail_active_descendants(
                        db_session,
                        parent_session_id=stale_session.session_id,
                        completed_at=datetime.now(UTC),
                        recovered_children=recovered_child_sessions,
                    )
                    recovered_ids.extend(child_ids)
                await db_session.commit()
            except Exception:
                await db_session.rollback()
                raise

        for recovered_id in recovered_ids:
            await self._evict_session_state(recovered_id)
        # Best-effort sync to Intaris for all recovered sessions (after commit)
        for stale_session in stale_sessions:
            if stale_session.session_id not in recovered_ids:
                continue
            if stale_session.parent_session_id is None:
                await self._sync_intaris_status(
                    stale_session.intaris_session_id or stale_session.session_id,
                    "idle",
                )
            else:
                await self._sync_intaris_status(
                    stale_session.intaris_session_id or stale_session.session_id,
                    "failed",
                )
        if recovered_ids:
            logger.info(
                "Recovered stale sessions",
                extra={"extra_data": {"recovered_count": len(recovered_ids)}},
            )
            if self.event_bus is not None:
                for recovered_id in recovered_ids:
                    await self.event_bus.publish(
                        Event(
                            type=EventType.SESSION_RECOVERED,
                            data={"session_id": recovered_id},
                        )
                    )
                follow_up_policy = FollowUpPolicy(llm=None)
                for child_session in recovered_child_sessions:
                    if str(getattr(child_session, "delegation_mode", "")) not in {
                        "delegate_async",
                        "delegate",
                    }:
                        continue
                    follow_up = follow_up_policy.build_delegation_follow_up(
                        conversation_id=child_session.conversation_id,
                        child_session_id=child_session.session_id,
                        status="failed",
                        result_summary="Background delegation stopped because the controller restarted.",
                    )
                    await self.event_bus.publish(
                        Event(
                            type=EventType.FOLLOW_UP_TURN_REQUESTED,
                            data={
                                "conversation_id": child_session.conversation_id,
                                "follow_up": follow_up.model_dump(mode="json"),
                                "channel_deliverable": False,
                                "delivery_id": None,
                                "delivery_fallback_text": None,
                            },
                        )
                    )
        return recovered_ids

    async def archive_conversation(self, conversation_id: str) -> bool:
        """Archive a conversation and complete its sessions."""

        return await self._close_conversation(conversation_id, conversation_status="archived")

    async def soft_delete_conversation(self, conversation_id: str) -> bool:
        """Soft-delete a conversation and complete its sessions."""

        return await self._close_conversation(conversation_id, conversation_status="deleted")

    async def purge_conversation(self, conversation_id: str) -> bool:
        """Hard-delete Cognis metadata for a conversation.

        Note: Intaris event-store purge is intentionally deferred until a verified
        delete-session provider contract exists.
        """

        async with self.session_factory() as db_session:
            try:
                sessions = await queries.list_conversation_sessions(db_session, conversation_id)
                deleted_sessions = await queries.delete_sessions_for_conversation(
                    db_session, conversation_id
                )
                deleted_conversations = await queries.delete_conversation(
                    db_session, conversation_id
                )
                await db_session.commit()
            except Exception:
                await db_session.rollback()
                raise

        if deleted_sessions or deleted_conversations:
            for session_row in sessions:
                await self._evict_session_state(session_row.session_id)
        return deleted_conversations > 0

    async def _close_conversation(self, conversation_id: str, conversation_status: str) -> bool:
        async with self.session_factory() as db_session:
            try:
                conversation = await queries.get_conversation(db_session, conversation_id)
                if conversation is None:
                    return False
                sessions = await queries.list_conversation_sessions(db_session, conversation_id)
                sessions_to_sync = [
                    session_row
                    for session_row in sessions
                    if session_row.status
                    not in {
                        SessionStatus.COMPLETED,
                        SessionStatus.FAILED,
                        SessionStatus.CANCELLED,
                        SessionStatus.TERMINATED,
                    }
                ]
                await queries.set_conversation_status(
                    db_session, conversation_id, conversation_status
                )
                await queries.update_conversation_active_session(db_session, conversation_id, None)
                for session_row in sessions:
                    if session_row.status in {
                        SessionStatus.COMPLETED,
                        SessionStatus.FAILED,
                        SessionStatus.CANCELLED,
                        SessionStatus.TERMINATED,
                    }:
                        continue
                    await queries.set_session_status(
                        db_session,
                        session_row.session_id,
                        SessionStatus.COMPLETED,
                        completed_at=datetime.now(UTC),
                        result_summary=f"conversation {conversation_status}",
                        completion_reason=f"conversation_{conversation_status}",
                    )
                await db_session.commit()
            except Exception:
                await db_session.rollback()
                raise

        for session_row in sessions:
            await self._evict_session_state(session_row.session_id)
        for session_row in sessions_to_sync:
            await self._sync_intaris_status(
                session_row.intaris_session_id or session_row.session_id,
                "completed",
                completion_reason=f"conversation_{conversation_status}",
            )
        return True

    async def _lookup_conversation_project_id(
        self, db_session: AsyncSession, conversation_id: str | None
    ) -> str | None:
        """Return the project_id for a conversation, if one is bound."""

        if not conversation_id:
            return None
        try:
            row = await queries.get_conversation(db_session, conversation_id)
        except Exception:
            return None
        return getattr(row, "project_id", None) if row is not None else None

    async def _require_agent(self, db_session: AsyncSession, agent_id: str) -> AgentDefinition:
        # System agents are Python constants, not in DB
        from cognis.core.agent_registry import SYSTEM_AGENTS

        if agent_id in SYSTEM_AGENTS:
            return SYSTEM_AGENTS[agent_id]

        agent_row = await queries.get_agent(db_session, agent_id)
        if agent_row is None:
            raise ValueError(f"Unknown agent: {agent_id}")
        return AgentDefinition(
            agent_id=agent_row.agent_id,
            owner_email=agent_row.owner_email,
            name=agent_row.name,
            display_name=agent_row.display_name,
            description=agent_row.description,
            system_prompt=agent_row.system_prompt,
            personality=agent_row.personality,
            skills=agent_row.skills,
            tools=agent_row.tools,
            permissions=agent_row.permissions,
            llm_config=agent_row.llm_config,
            execution=agent_row.execution,
            avatar_url=agent_row.avatar_url,
            avatar_image_id=getattr(agent_row, "avatar_image_id", None),
            status=agent_row.status,
            created_at=agent_row.created_at,
            updated_at=agent_row.updated_at,
        )

    def _build_root_intention(self, agent: AgentDefinition, title: str | None) -> str:
        return f"Conversation with {agent.name}"

    def _build_child_intention(self, agent: AgentDefinition, task_description: str) -> str:
        description_prefix = f"{agent.name}: " if agent.name else ""
        return f"{description_prefix}{task_description}".strip()

    async def _fail_active_descendants(
        self,
        db_session: AsyncSession,
        *,
        parent_session_id: str,
        completed_at: datetime,
        recovered_children: list[Any] | None = None,
    ) -> list[str]:
        recovered_ids: list[str] = []
        child_sessions = await queries.list_child_sessions(db_session, parent_session_id)
        for child_session in child_sessions:
            if child_session.status == "active":
                await queries.set_session_status(
                    db_session,
                    child_session.session_id,
                    "failed",
                    completed_at=completed_at,
                    result_summary="controller restart; parent recovered",
                )
                recovered_ids.append(child_session.session_id)
                if recovered_children is not None:
                    recovered_children.append(child_session)
            recovered_ids.extend(
                await self._fail_active_descendants(
                    db_session,
                    parent_session_id=child_session.session_id,
                    completed_at=completed_at,
                    recovered_children=recovered_children,
                )
            )
        return recovered_ids


def _to_conversation_model(row: Any) -> ConversationModel:
    return ConversationModel(
        conversation_id=row.conversation_id,
        user_email=row.user_email,
        agent_id=row.agent_id,
        title=row.title,
        title_source=getattr(row, "title_source", "unset") or "unset",
        context=ConversationContext(
            type=row.context_type,
            ref=row.context_ref,
            platform_data=row.context_data or {},
            memory_labels=row.memory_labels or {},
        ),
        project_id=getattr(row, "project_id", None),
        active_session_id=row.active_session_id,
        active_executor_id=getattr(row, "active_executor_id", None),
        active_executor_assigned_at=getattr(row, "active_executor_assigned_at", None),
        active_executor_expires_at=getattr(row, "active_executor_expires_at", None),
        active_executor_source=getattr(row, "active_executor_source", None),
        starred_at=getattr(row, "starred_at", None),
        status=row.status,
        last_message_at=row.last_message_at,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _to_session_model(row: Any) -> SessionModel:
    return SessionModel(
        session_id=row.session_id,
        conversation_id=row.conversation_id,
        parent_session_id=row.parent_session_id,
        previous_session_id=getattr(row, "previous_session_id", None),
        user_email=row.user_email,
        agent_id=row.agent_id,
        delegation_mode=row.delegation_mode,
        delegation_task=row.delegation_task,
        status=row.status,
        completion_reason=getattr(row, "completion_reason", None),
        intaris_session_id=row.intaris_session_id,
        mnemory_session_id=row.mnemory_session_id,
        started_at=row.started_at,
        idle_since=row.idle_since,
        completed_at=row.completed_at,
        result_summary=row.result_summary,
        result_content=getattr(row, "result_content", None),
        updated_at=row.updated_at,
    )
