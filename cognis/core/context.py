"""Context assembly for chat turns."""

from __future__ import annotations

import asyncio
import datetime
import html
import json
import re
from hashlib import sha256
from pathlib import Path
from time import monotonic
from typing import Any

from prometheus_client import Counter
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from cognis.core.attachment_utils import (
    attachment_label as _attachment_label,
)
from cognis.core.attachment_utils import (
    attachment_note as _attachment_note,
)
from cognis.core.attachment_utils import (
    attachment_placeholder_text as _attachment_placeholder_text,
)
from cognis.core.context_budget import resolve_context_budget
from cognis.core.context_projection import project_messages
from cognis.core.errors import ImmutablePrefixUnavailable
from cognis.core.followups import (
    FollowUpMetadata,
    build_history_boundary_message,
    render_follow_up_block,
)
from cognis.core.immutable_prefix import (
    ImmutablePrefixEntry,
    build_context_snapshot_event,
    build_prefix_message_events,
    sort_prefix_entries,
)
from cognis.core.prompts import PromptContext, build_critical_rules, build_system_instructions
from cognis.core.runtime import ExecutorEnvironmentSnapshot, build_local_executor_environment
from cognis.core.title_policy import sync_intaris_title
from cognis.logging import get_logger
from cognis.models.agent import AgentDefinition
from cognis.models.artifact import ArtifactKind
from cognis.models.session import ConversationModel, SessionModel
from cognis.models.tool import Permission, ToolDefinition
from cognis.runtime_context import scoped_runtime_context
from cognis.store.queries import get_setting_value

logger = get_logger(__name__)

MNEMORY_SESSION_REPAIRED_TOTAL = Counter(
    "cognis_mnemory_session_repaired_total",
    "Mnemory session repairs performed while rebuilding immutable prefixes.",
    labelnames=("reason",),
)

EVENT_TYPES_FOR_CONTEXT = [
    "user_message",
    "assistant_message",
    "tool_call",
    "tool_result",
    "delegation",
    "lifecycle",
    "evaluation",
]

_MAX_PROJECT_INSTRUCTION_BYTES = 32000
_VISIBLE_HISTORY_EVENT_TYPES = {
    "system_message",
    "developer_message",
    "user_message",
    "assistant_message",
}


def _is_newer_timestamp(candidate: str | None, current: str | None) -> bool:
    """Return True when *candidate* is newer than *current*.

    Missing candidates are never newer. Missing current values are always older.
    Falls back to lexical comparison for malformed timestamps.
    """

    if not candidate:
        return False
    if not current:
        return True
    try:
        return datetime.datetime.fromisoformat(candidate) >= datetime.datetime.fromisoformat(
            current
        )
    except ValueError:
        return candidate >= current


class ContextAssemblyResult(BaseModel):
    """Fully assembled LLM context and metadata."""

    messages: list[dict[str, Any]]
    audit_messages: list[dict[str, Any]] = Field(default_factory=list)
    degraded: bool = False
    degraded_sources: list[str] = Field(default_factory=list)
    resolved_model: str
    static_tokens: int = 0
    dynamic_tokens: int = 0
    prompt_tokens: int = 0
    max_context_tokens: int = 0
    recommend_compaction: bool = False
    cache_breakpoint_index: int | None = None


def _audit_hash(role: str, content: str, source: str) -> str:
    payload = json.dumps(
        {"role": role, "content": content, "source": source},
        sort_keys=True,
        separators=(",", ":"),
    )
    return sha256(payload.encode("utf-8")).hexdigest()


def _filter_attachments_by_names(
    attachments: list[dict[str, Any]],
    names: list[str],
) -> list[dict[str, Any]]:
    wanted = set(names)
    return [
        attachment
        for attachment in attachments
        if str(attachment.get("filename") or attachment.get("artifact_id") or "attachment")
        in wanted
    ]


def _native_attachment_blocks(
    attachments: list[dict[str, Any]],
    model_info: Any,
) -> tuple[list[dict[str, Any]], list[str]]:
    blocks: list[dict[str, Any]] = []
    unsupported: list[str] = []
    for attachment in attachments:
        kind = str(attachment.get("kind") or ArtifactKind.FILE.value)
        url = attachment.get("url")
        filename = str(attachment.get("filename") or attachment.get("artifact_id") or "attachment")
        if not isinstance(url, str) or not url:
            unsupported.append(filename)
            continue
        if kind == ArtifactKind.IMAGE.value and getattr(model_info, "supports_vision", False):
            blocks.append({"type": "image_url", "image_url": {"url": url}})
            continue
        if kind == ArtifactKind.PDF.value and (
            getattr(model_info, "supports_pdf_input", False)
            or getattr(model_info, "supports_file_input", False)
        ):
            blocks.append({"type": "file", "file": {"file_url": url, "filename": filename}})
            continue
        if kind == ArtifactKind.AUDIO.value and (
            getattr(model_info, "supports_audio_input", False)
            or getattr(model_info, "supports_file_input", False)
        ):
            blocks.append({"type": "file", "file": {"file_url": url, "filename": filename}})
            continue
        if kind in {ArtifactKind.FILE.value, ArtifactKind.VIDEO.value} and getattr(
            model_info, "supports_file_input", False
        ):
            blocks.append({"type": "file", "file": {"file_url": url, "filename": filename}})
            continue
        unsupported.append(filename)
    return blocks, unsupported


def _current_turn_attachment_message(
    *,
    user_message: str,
    user_message_role: str,
    user_attachments: list[dict[str, Any]] | None,
    model_info: Any,
    include_user_message: bool,
) -> dict[str, Any] | None:
    attachments = user_attachments or []
    if not attachments:
        if not include_user_message:
            return None
        if user_message or user_message_role != "system":
            return {"role": user_message_role, "content": user_message}
        return None

    attachment_blocks, unsupported = _native_attachment_blocks(attachments, model_info)
    if attachment_blocks:
        blocks: list[dict[str, Any]] = []
        if include_user_message:
            intro = user_message
            note = _attachment_note(attachments)
            intro = f"{user_message}\n\n{note}" if user_message.strip() else note
        else:
            intro = _attachment_note(attachments)
        if intro:
            blocks.append({"type": "text", "text": intro})
        else:
            blocks.append(
                {
                    "type": "text",
                    "text": _attachment_placeholder_text(
                        str(attachment.get("kind") or ArtifactKind.FILE.value)
                        for attachment in attachments
                    ),
                }
            )
        blocks.extend(attachment_blocks)
        if unsupported:
            blocks.append(
                {
                    "type": "text",
                    "text": _attachment_note(
                        _filter_attachments_by_names(attachments, unsupported)
                    ),
                }
            )
        return {"role": user_message_role, "content": blocks}

    if include_user_message:
        note = _attachment_note(attachments)
        content = f"{user_message}\n\n{note}" if user_message.strip() else note
        if content or user_message_role != "system":
            return {"role": user_message_role, "content": content}
    return None


def _build_environment_info(
    executor_environment: ExecutorEnvironmentSnapshot | None = None,
    *,
    workspace_root: str | None = None,
    effective_working_directory: str | None = None,
) -> str:
    """Build environment information for the LLM context.

    Provides the LLM with the selected tool executor environment so it
    generates correct absolute paths in tool calls instead of guessing.
    """

    env = executor_environment or _local_environment_snapshot()
    if not env.available:
        executor_label = env.executor_id or "selected remote executor"
        executor_type = env.executor_type or "remote"
        return (
            "Environment:\n"
            f"- Executor: {executor_label} ({executor_type})\n"
            "- Environment details: unavailable from this executor\n"
            "If you omit a filesystem path or shell workdir, the executor still defaults "
            "to its own local home directory. Do not guess controller paths. "
            "If you need the current date or time, call get_current_datetime."
        )

    return (
        "Environment:\n"
        + (_format_executor_label(env))
        + f"- Platform: {env.platform_os or 'unknown'} ({env.platform_arch or 'unknown'})\n"
        + f"- Hostname: {env.hostname or 'unknown'}\n"
        + f"- System user: {env.user or 'unknown'}\n"
        + f"- Home directory: {env.home or 'unknown'}\n"
        + f"- Working directory: {env.cwd or 'unknown'}\n"
        + (f"- Workspace root: {workspace_root}\n" if workspace_root else "")
        + (
            f"- Effective working directory: {effective_working_directory}\n"
            if effective_working_directory
            else ""
        )
        + "When the user references ~ or $HOME, use the home directory above. "
        + "If a filesystem path or shell workdir is omitted, tools default to the effective "
        + "working directory above when available. If you need the current date or time, "
        + "call get_current_datetime."
    )


def _local_environment_snapshot() -> ExecutorEnvironmentSnapshot:
    """Build a local environment snapshot for controller/in-process usage."""

    return build_local_executor_environment(source="context_local_fallback")


def _format_executor_label(env: ExecutorEnvironmentSnapshot) -> str:
    if env.executor_id and env.executor_type:
        return f"- Executor: {env.executor_id} ({env.executor_type})\n"
    if env.executor_id:
        return f"- Executor: {env.executor_id}\n"
    if env.executor_type:
        return f"- Executor type: {env.executor_type}\n"
    return ""


def _compose_identity_prompt(agent: AgentDefinition) -> str | None:
    """Compose the immutable identity prompt for an agent."""
    identity_parts = [part for part in [agent.compose_personality(), agent.system_prompt] if part]
    return "\n\n".join(identity_parts) if identity_parts else None


def _format_compaction_summary(compaction_summary: str | None) -> str | None:
    """Format the stable continuation summary for the immutable prefix."""
    if not compaction_summary:
        return None
    return (
        "This is a continuation from a previous session. "
        "Here is a summary of what was discussed:\n\n"
        f"{compaction_summary}\n\n"
        "Use this summary as background context for the continuation that follows."
    )


def _tagged_section(tag: str, content: str | None) -> str | None:
    """Wrap stable prompt content in a simple XML-style section tag."""
    if not content:
        return None
    return f"<{tag}>\n{content}\n</{tag}>"


def _load_project_instructions(
    *,
    workspace_root: str | None,
    effective_working_directory: str | None,
    executor_environment: ExecutorEnvironmentSnapshot | None,
) -> list[str]:
    executor_type = executor_environment.executor_type if executor_environment else None
    if executor_type not in {None, "in_process", "subprocess", "controller"}:
        return []
    root = workspace_root or effective_working_directory
    if not root:
        return []
    root_path = Path(root).resolve()
    if not root_path.is_dir():
        return []
    directories: list[Path] = []
    if effective_working_directory:
        current = Path(effective_working_directory).resolve()
        while True:
            directories.append(current)
            if current == root_path or current.parent == current:
                break
            current = current.parent
    else:
        directories.append(root_path)

    seen_dirs: set[Path] = set()
    for directory in directories:
        if directory in seen_dirs or not directory.is_dir():
            continue
        seen_dirs.add(directory)
        for filename in ("AGENTS.md", "CLAUDE.md", "README.md"):
            candidate = directory / filename
            if not candidate.is_file():
                continue
            try:
                content = candidate.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if not content.strip():
                continue
            return [f"Instructions from: {candidate}\n{content[:_MAX_PROJECT_INSTRUCTION_BYTES]}"]
    return []


class ContextAssembler:
    """Assemble LLM prompt context from cache, memory, and session state."""

    def __init__(
        self,
        *,
        memory: Any,
        guardrails: Any,
        llm: Any,
        session_cache: Any,
        session_manager: Any,
        max_context_tokens: int | None = None,
        compaction_threshold: float,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
    ) -> None:
        self.memory = memory
        self.guardrails = guardrails
        self.llm = llm
        self.session_cache = session_cache
        self.session_manager = session_manager
        del max_context_tokens
        self.compaction_threshold = compaction_threshold
        self.session_factory = session_factory

    @classmethod
    async def from_session_factory(
        cls,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        memory: Any,
        guardrails: Any,
        llm: Any,
        session_cache: Any,
        session_manager: Any,
    ) -> ContextAssembler:
        """Create a context assembler with DB-backed settings."""

        async with session_factory() as db_session:
            compaction_threshold = await get_setting_value(
                db_session, "session.compaction_threshold", 0.85
            )
        return cls(
            memory=memory,
            guardrails=guardrails,
            llm=llm,
            session_cache=session_cache,
            session_manager=session_manager,
            compaction_threshold=float(compaction_threshold)
            if isinstance(compaction_threshold, (int, float))
            else 0.85,
            session_factory=session_factory,
        )

    async def assemble(
        self,
        *,
        session: SessionModel,
        conversation: ConversationModel,
        agent: AgentDefinition,
        user_message: str,
        user_attachments: list[dict[str, Any]] | None = None,
        attachment_notice: str | None = None,
        attachment_context: str | None = None,
        user_message_role: str = "user",
        tool_definitions: list[ToolDefinition] | None = None,
        active_delegations: list[dict[str, Any]] | None = None,
        prior_context: list[dict[str, Any]] | None = None,
        follow_up: FollowUpMetadata | None = None,
        routing_reminder: str | None = None,
        skip_user_message: bool = False,
        skip_memory: bool = False,
        prompt_context: PromptContext = PromptContext.CHAT,
        executor_environment: ExecutorEnvironmentSnapshot | None = None,
        workspace_root: str | None = None,
        effective_working_directory: str | None = None,
        include_project_context: bool = True,
    ) -> ContextAssemblyResult:
        """Build the LLM message list for a single turn.

        ``prior_context`` is an optional list of messages to inject after
        session history and before the user message.  Used by the workflow
        engine to inject prior step output.  Chat turns pass ``None``.

        ``routing_reminder`` is ephemeral turn-local system guidance for the
        current chat turn only. It must not be persisted to session history,
        DB state, or audit content.

        ``skip_memory`` skips Mnemory recall and memory instructions.
        Used for secondary agents that don't have memory integration.

        ``prompt_context`` selects which system instructions to inject
        (chat routing, step execution, or delegation focus).
        """

        logger.debug(
            "context: assembly started",
            extra={"extra_data": {"session_id": session.session_id, "agent_id": agent.agent_id}},
        )

        # --- Secondary agents: skip memory and intention ---
        if skip_memory:
            return await self._assemble_without_memory(
                session=session,
                conversation=conversation,
                agent=agent,
                user_message=user_message,
                user_attachments=user_attachments,
                attachment_notice=attachment_notice,
                attachment_context=attachment_context,
                user_message_role=user_message_role,
                tool_definitions=tool_definitions,
                active_delegations=active_delegations,
                prior_context=prior_context,
                follow_up=follow_up,
                routing_reminder=routing_reminder,
                skip_user_message=skip_user_message,
                prompt_context=prompt_context,
                executor_environment=executor_environment,
                workspace_root=workspace_root,
                effective_working_directory=effective_working_directory,
                include_project_context=include_project_context,
            )

        cached_intention = self.session_cache.get_intention(session.session_id)
        refreshed_cache_entry: Any | None = None
        performed_refresh = False
        if (
            not self.session_cache.get_prefix_entries(session.session_id)
            and session.mnemory_session_id is not None
        ):
            refreshed_cache_entry = await self.session_cache.refresh(session)
            performed_refresh = True
            if not self.session_cache.get_prefix_entries(session.session_id):
                await self.session_cache.mark_prefix_repair_needed(session.session_id)
        prefix_entries = await self._ensure_immutable_prefix(
            session=session,
            agent=agent,
            project_instructions=[],
            memory_labels=conversation.context.memory_labels,
            context=cached_intention,
        )

        with scoped_runtime_context(user_email=session.user_email, agent_id=session.agent_id):
            recall_task = self.memory.recall(
                query=user_message,
                session_id=session.mnemory_session_id,
                labels=conversation.context.memory_labels,
                context=cached_intention,
                search_mode="search",
                include_instructions=False,
                managed=True,
                instruction_mode=None,
            )
            intention_task = self.guardrails.get_session(
                session.intaris_session_id or session.session_id
            )
            if performed_refresh:
                recall_result, intention_result = await asyncio.gather(
                    recall_task,
                    intention_task,
                    return_exceptions=True,
                )
                cache_result: Any = refreshed_cache_entry
            else:
                refresh_task = self.session_cache.refresh(session)
                # Providers are mandatory — any failure propagates immediately.
                # Intention is best-effort (non-critical for turn execution).
                gathered_results: tuple[Any, Any, Any] = await asyncio.gather(
                    recall_task,
                    refresh_task,
                    intention_task,
                    return_exceptions=True,
                )
                recall_result, cache_result, intention_result = gathered_results

        degraded_sources: list[str] = []

        # Mnemory is mandatory — raise if recall failed
        if isinstance(recall_result, Exception):
            logger.error(
                "context: Mnemory recall failed (mandatory provider)",
                extra={"extra_data": {"session_id": session.session_id}},
                exc_info=recall_result,
            )
            raise recall_result

        # Intaris event refresh is mandatory — raise if failed
        if isinstance(cache_result, Exception):
            # Allow if we have a warm cache from a previous refresh
            cache_entry = self.session_cache.get_entry(session.session_id)
            if cache_entry is None or not cache_entry.initialized:
                logger.error(
                    "context: Intaris event refresh failed (mandatory provider, no cache)",
                    extra={"extra_data": {"session_id": session.session_id}},
                    exc_info=cache_result,
                )
                raise cache_result
            logger.warning(
                "context: Intaris refresh failed but cache is warm, continuing",
                extra={"extra_data": {"session_id": session.session_id}},
            )
            degraded_sources.append("events")
        else:
            cache_entry = cache_result

        # Intention is best-effort (non-critical)
        if isinstance(intention_result, Exception):
            logger.warning(
                "context: intention fetch failed (non-critical)",
                extra={"extra_data": {"session_id": session.session_id}},
            )
            degraded_sources.append("intention")
        else:
            cached_updated_at = getattr(cache_entry, "intention_updated_at", None)
            if _is_newer_timestamp(intention_result.updated_at, cached_updated_at):
                await self.session_cache.update_intention(
                    session.session_id,
                    intention_result.intention,
                    updated_at=intention_result.updated_at,
                )
                cache_entry.intention = intention_result.intention
                cache_entry.intention_updated_at = intention_result.updated_at

            if intention_result.title and self.session_factory is not None:
                try:
                    async with self.session_factory() as db_session:
                        ok = await sync_intaris_title(
                            db_session, conversation, intention_result.title
                        )
                        if ok:
                            await db_session.commit()
                except Exception:
                    logger.debug(
                        "context: failed to sync title from Intaris",
                        extra={
                            "extra_data": {
                                "conversation_id": conversation.conversation_id,
                            }
                        },
                        exc_info=True,
                    )

        # ----- Memory handling: split into immutable and mutable parts -----
        mutable_search_results: str | None = None

        # recall_result is guaranteed to be a dict here (we raise on Exception above).
        recall_payload: dict[str, Any] = recall_result
        recall_session_id = str(recall_payload.get("session_id") or "").strip()
        if (
            recall_session_id
            and session.mnemory_session_id is not None
            and recall_session_id != session.mnemory_session_id
        ):
            logger.warning(
                "context: per-turn recall returned unexpected Mnemory session id",
                extra={
                    "extra_data": {
                        "session_id": session.session_id,
                        "expected_mnemory_session_id": session.mnemory_session_id,
                        "returned_mnemory_session_id": recall_session_id,
                    }
                },
            )
            adopted = await self._adopt_mnemory_session(session, recall_session_id)
            if adopted:
                await self.session_cache.mark_prefix_repair_needed(session.session_id)

        # Format mutable search results
        mutable_search_results = _format_search_results(recall_payload.get("search_results"))

        # Model resolution chain: session override → agent config → system default
        model_override = self.session_cache.get_model_override(session.session_id)
        explicit_model = model_override or (agent.llm_config.model if agent.llm_config else None)
        explicit_provider_id = agent.llm_config.provider_id if agent.llm_config else None
        provider_id: str | None = None
        if hasattr(self.llm, "resolve_model_target"):
            try:
                resolved_model, provider_id = await self.llm.resolve_model_target(
                    explicit_model=explicit_model,
                    task_type="default",
                    explicit_provider_id=explicit_provider_id,
                )
            except TypeError:
                resolved_model, provider_id = await self.llm.resolve_model_target(
                    explicit_model=explicit_model,
                    task_type="default",
                )
        else:
            try:
                resolved_model = await self.llm.resolve_model(
                    explicit_model=explicit_model,
                    task_type="default",
                    explicit_provider_id=explicit_provider_id,
                )
            except TypeError:
                resolved_model = await self.llm.resolve_model(
                    explicit_model=explicit_model,
                    task_type="default",
                )
        if provider_id is not None:
            try:
                model_info = await self.llm.get_model_info(resolved_model, provider_id=provider_id)
            except TypeError:
                model_info = await self.llm.get_model_info(resolved_model)
        else:
            model_info = await self.llm.get_model_info(resolved_model)
        if model_info.model_id == "unknown":
            degraded_sources.append("model_info")

        budget = resolve_context_budget(
            max_context_tokens=model_info.context_window,
            agent_max_tokens=(agent.llm_config.max_tokens if agent.llm_config else None),
            model_max_output_tokens=model_info.max_output_tokens,
        )
        max_context_tokens = budget.max_context_tokens
        reserve_output_tokens = budget.effective_reserve_output_tokens
        immutable_prefix = self._compose_immutable_prefix(
            agent=agent,
            prompt_context=prompt_context,
            prefix_entries=prefix_entries,
            resolved_model=resolved_model,
        )
        system_prompt_tokens, tool_schema_tokens = self._count_static_tokens(
            resolved_model=resolved_model,
            immutable_prefix=immutable_prefix,
            tool_definitions=tool_definitions or [],
        )
        max_prompt_tokens = max(0, max_context_tokens - reserve_output_tokens)
        if immutable_prefix and system_prompt_tokens + tool_schema_tokens > max_prompt_tokens:
            immutable_prefix = self._cap_prefix_section(
                immutable_prefix,
                resolved_model,
                max(0, max_prompt_tokens - tool_schema_tokens),
            )
            system_prompt_tokens, tool_schema_tokens = self._count_static_tokens(
                resolved_model=resolved_model,
                immutable_prefix=immutable_prefix,
                tool_definitions=tool_definitions or [],
            )
        static_tokens = system_prompt_tokens + tool_schema_tokens
        dynamic_tokens = max(0, max_context_tokens - static_tokens - reserve_output_tokens)

        # ----- Build messages: immutable prefix first, then mutable suffix -----
        messages: list[dict[str, Any]] = []

        if immutable_prefix:
            messages.append(
                {"role": "system", "content": immutable_prefix, "_immutable_prefix": True}
            )

        if include_project_context:
            messages.extend(self._project_context_messages(session.session_id))

        # ----- Mutable suffix -----

        messages.append(
            {
                "role": "system",
                "content": _build_environment_info(
                    executor_environment,
                    workspace_root=workspace_root,
                    effective_working_directory=effective_working_directory,
                ),
                "_audit_source": "environment_info",
                "_audit_role": "system",
            }
        )

        # History messages (append-only)
        history_messages = self._events_to_messages(
            self.session_cache.get_events_since_compaction(
                session.session_id, EVENT_TYPES_FOR_CONTEXT
            )
        )
        messages.extend(history_messages)

        if active_delegations:
            messages.append(
                {
                    "role": "system",
                    "content": _format_active_delegations(active_delegations),
                    "_audit_source": "delegation_result",
                    "_audit_role": "developer",
                }
            )

        # Prior context from caller (e.g. prior workflow step output).
        # Mark these messages so _prune_messages can protect them.
        if prior_context:
            for msg in prior_context:
                msg["_prior_context"] = True
                msg.setdefault("_audit_source", "workflow_step_context")
                msg.setdefault("_audit_role", msg.get("role", "developer"))
            messages.extend(prior_context)
            logger.debug(
                "context: injecting prior step context",
                extra={"extra_data": {"message_count": len(prior_context)}},
            )

        if follow_up is not None:
            messages.append(
                {
                    "role": "system",
                    "content": build_history_boundary_message(),
                    "_follow_up_context": True,
                    "_audit_source": "follow_up_boundary",
                    "_audit_role": "developer",
                }
            )
            messages.append(
                {
                    "role": "system",
                    "content": render_follow_up_block(follow_up),
                    "_follow_up_context": True,
                    "_audit_source": "follow_up_boundary",
                    "_audit_role": "developer",
                }
            )

        # Detect whether the current user_message was already recorded into
        # the Intaris history (turn_scheduler / agent_loop record it early so
        # the intention barrier can start updating in parallel). If so, it is
        # already present as the trailing user-role message in history and
        # must not be re-appended, or the LLM sees the prompt twice.
        already_in_history = _current_user_message_already_in_history(
            history_messages,
            user_message=user_message,
            user_message_role=user_message_role,
            user_attachments=user_attachments,
        )

        if mutable_search_results:
            messages.append(
                {
                    "role": "system",
                    "content": (
                        '<memory_context trust="untrusted">\n'
                        "Recalled memories:\n" + mutable_search_results + "\n</memory_context>"
                    ),
                    "_audit_source": "memory_search",
                    "_audit_role": "developer",
                }
            )

        if not skip_user_message and not already_in_history:
            if routing_reminder:
                messages.append(
                    {
                        "role": "system",
                        "content": routing_reminder,
                        "_routing_reminder": True,
                        "_audit_source": "routing_reminder",
                        "_audit_role": "developer",
                    }
                )
            if attachment_notice:
                messages.append(
                    {
                        "role": "system",
                        "content": attachment_notice,
                        "_audit_source": "attachment_notice",
                        "_audit_role": "developer",
                    }
                )
            if attachment_context:
                messages.append(
                    {
                        "role": "user",
                        "content": attachment_context,
                        "_audit_source": "attachment_notice",
                        "_audit_role": "user",
                    }
                )
            current_turn_message = _current_turn_attachment_message(
                user_message=user_message,
                user_message_role=user_message_role,
                user_attachments=user_attachments,
                model_info=model_info,
                include_user_message=True,
            )
            if current_turn_message is not None:
                messages.append(current_turn_message)
        elif already_in_history:
            # Prompt already recorded in history; still surface any
            # turn-local signals that were meant to accompany it. The
            # prompt-replay side does not carry these because they are
            # ephemeral (not persisted to history).
            if routing_reminder:
                messages.append(
                    {
                        "role": "system",
                        "content": routing_reminder,
                        "_routing_reminder": True,
                        "_audit_source": "routing_reminder",
                        "_audit_role": "developer",
                    }
                )
            if attachment_notice:
                # Controller diagnostic about unsupported attachments for
                # this model — must survive dedupe or the model is not
                # told why files are missing from the request.
                messages.append(
                    {
                        "role": "system",
                        "content": attachment_notice,
                        "_audit_source": "attachment_notice",
                        "_audit_role": "developer",
                    }
                )
            if attachment_context:
                messages.append(
                    {
                        "role": "user",
                        "content": attachment_context,
                        "_audit_source": "attachment_notice",
                        "_audit_role": "user",
                    }
                )
            current_turn_attachments = _current_turn_attachment_message(
                user_message=user_message,
                user_message_role=user_message_role,
                user_attachments=user_attachments,
                model_info=model_info,
                include_user_message=False,
            )
            if current_turn_attachments is not None:
                messages.append(current_turn_attachments)

        projection = project_messages(messages)
        messages = self._prune_messages(
            messages=projection.messages,
            resolved_model=resolved_model,
            max_prompt_tokens=max_prompt_tokens,
            tool_schema_tokens=tool_schema_tokens,
        )

        audit_messages = self._collect_audit_messages(messages)

        # Recompute cache breakpoint after pruning while internal markers are still present.
        cache_breakpoint_index = _find_cache_breakpoint(messages)

        # Strip internal markers before sending to LLM
        for msg in messages:
            msg.pop("_immutable_prefix", None)
            msg.pop("_project_context", None)
            msg.pop("_prior_context", None)
            msg.pop("_follow_up_context", None)
            msg.pop("_routing_reminder", None)
            msg.pop("_audit_source", None)
            msg.pop("_audit_role", None)

        prompt_tokens = (
            self.llm.count_messages_tokens(messages, resolved_model) + tool_schema_tokens
        )
        recommend_compaction = (
            budget.available_prompt_tokens > 0
            and (prompt_tokens / budget.available_prompt_tokens) >= self.compaction_threshold
        )
        logger.info(
            "context: assembly completed",
            extra={
                "extra_data": {
                    "session_id": session.session_id,
                    "degraded": bool(degraded_sources),
                    "degraded_sources": sorted(set(degraded_sources)),
                    "prompt_tokens": prompt_tokens,
                    "recommend_compaction": recommend_compaction,
                    "cache_breakpoint_index": cache_breakpoint_index,
                }
            },
        )
        return ContextAssemblyResult(
            messages=messages,
            audit_messages=audit_messages,
            degraded=bool(degraded_sources),
            degraded_sources=sorted(set(degraded_sources)),
            resolved_model=resolved_model,
            static_tokens=static_tokens,
            dynamic_tokens=dynamic_tokens,
            prompt_tokens=prompt_tokens,
            max_context_tokens=max_context_tokens,
            recommend_compaction=recommend_compaction,
            cache_breakpoint_index=cache_breakpoint_index,
        )

    async def _assemble_without_memory(
        self,
        *,
        session: SessionModel,
        conversation: ConversationModel,
        agent: AgentDefinition,
        user_message: str,
        user_attachments: list[dict[str, Any]] | None = None,
        attachment_notice: str | None = None,
        attachment_context: str | None = None,
        user_message_role: str = "user",
        tool_definitions: list[ToolDefinition] | None = None,
        active_delegations: list[dict[str, Any]] | None = None,
        prior_context: list[dict[str, Any]] | None = None,
        follow_up: FollowUpMetadata | None = None,
        routing_reminder: str | None = None,
        skip_user_message: bool = False,
        prompt_context: PromptContext = PromptContext.TASK_STEP,
        executor_environment: ExecutorEnvironmentSnapshot | None = None,
        workspace_root: str | None = None,
        effective_working_directory: str | None = None,
        include_project_context: bool = True,
    ) -> ContextAssemblyResult:
        """Assemble context without Mnemory calls — for secondary agents.

        Skips: Mnemory recall, memory instructions, core memories,
        recalled memories, intention fetch. Keeps: system prompt,
        system instructions, compaction summary, history, prior step context.
        """
        degraded_sources: list[str] = []

        # Still need Intaris event refresh for history
        cache_result = await self.session_cache.refresh(session)
        if isinstance(cache_result, Exception):
            cache_entry = self.session_cache.get_entry(session.session_id)
            if cache_entry is None or not cache_entry.initialized:
                raise cache_result
            degraded_sources.append("events")
        else:
            cache_entry = cache_result

        # Model resolution
        model_override = self.session_cache.get_model_override(session.session_id)
        explicit_model = model_override or (agent.llm_config.model if agent.llm_config else None)
        explicit_provider_id = agent.llm_config.provider_id if agent.llm_config else None
        provider_id: str | None = None
        if hasattr(self.llm, "resolve_model_target"):
            try:
                resolved_model, provider_id = await self.llm.resolve_model_target(
                    explicit_model=explicit_model,
                    task_type="default",
                    explicit_provider_id=explicit_provider_id,
                )
            except TypeError:
                resolved_model, provider_id = await self.llm.resolve_model_target(
                    explicit_model=explicit_model,
                    task_type="default",
                )
        else:
            try:
                resolved_model = await self.llm.resolve_model(
                    explicit_model=explicit_model,
                    task_type="default",
                    explicit_provider_id=explicit_provider_id,
                )
            except TypeError:
                resolved_model = await self.llm.resolve_model(
                    explicit_model=explicit_model,
                    task_type="default",
                )
        if provider_id is not None:
            try:
                model_info = await self.llm.get_model_info(resolved_model, provider_id=provider_id)
            except TypeError:
                model_info = await self.llm.get_model_info(resolved_model)
        else:
            model_info = await self.llm.get_model_info(resolved_model)
        if model_info.model_id == "unknown":
            degraded_sources.append("model_info")

        budget = resolve_context_budget(
            max_context_tokens=model_info.context_window,
            agent_max_tokens=(agent.llm_config.max_tokens if agent.llm_config else None),
            model_max_output_tokens=model_info.max_output_tokens,
        )
        max_context_tokens = budget.max_context_tokens
        reserve_output_tokens = budget.effective_reserve_output_tokens
        prefix_entries = await self._ensure_immutable_prefix(
            session=session,
            agent=agent,
            project_instructions=[],
            memory_labels=conversation.context.memory_labels,
            context=None,
            allow_empty_memory=True,
        )
        immutable_prefix = self._compose_immutable_prefix(
            agent=agent,
            prompt_context=prompt_context,
            prefix_entries=prefix_entries,
            resolved_model=resolved_model,
        )
        system_prompt_tokens, tool_schema_tokens = self._count_static_tokens(
            resolved_model=resolved_model,
            immutable_prefix=immutable_prefix,
            tool_definitions=tool_definitions or [],
        )
        max_prompt_tokens = max(0, max_context_tokens - reserve_output_tokens)
        if immutable_prefix and system_prompt_tokens + tool_schema_tokens > max_prompt_tokens:
            immutable_prefix = self._cap_prefix_section(
                immutable_prefix,
                resolved_model,
                max(0, max_prompt_tokens - tool_schema_tokens),
            )
            system_prompt_tokens, tool_schema_tokens = self._count_static_tokens(
                resolved_model=resolved_model,
                immutable_prefix=immutable_prefix,
                tool_definitions=tool_definitions or [],
            )
        static_tokens = system_prompt_tokens + tool_schema_tokens
        dynamic_tokens = max(0, max_context_tokens - static_tokens - reserve_output_tokens)

        # Build messages: immutable prefix + env + history
        messages: list[dict[str, Any]] = []

        if immutable_prefix:
            messages.append(
                {"role": "system", "content": immutable_prefix, "_immutable_prefix": True}
            )

        if include_project_context:
            messages.extend(self._project_context_messages(session.session_id))

        messages.append(
            {
                "role": "system",
                "content": _build_environment_info(
                    executor_environment,
                    workspace_root=workspace_root,
                    effective_working_directory=effective_working_directory,
                ),
                "_audit_source": "environment_info",
                "_audit_role": "system",
            }
        )

        history_messages = self._events_to_messages(
            self.session_cache.get_events_since_compaction(
                session.session_id, EVENT_TYPES_FOR_CONTEXT
            )
        )
        messages.extend(history_messages)

        # No recalled memories (skip_memory)
        # No active delegations (secondary agents don't delegate)

        if prior_context:
            for msg in prior_context:
                msg["_prior_context"] = True
                msg.setdefault("_audit_source", "workflow_step_context")
                msg.setdefault("_audit_role", msg.get("role", "developer"))
            messages.extend(prior_context)

        if follow_up is not None:
            messages.append(
                {
                    "role": "system",
                    "content": build_history_boundary_message(),
                    "_follow_up_context": True,
                    "_audit_source": "follow_up_boundary",
                    "_audit_role": "developer",
                }
            )
            messages.append(
                {
                    "role": "system",
                    "content": render_follow_up_block(follow_up),
                    "_follow_up_context": True,
                    "_audit_source": "follow_up_boundary",
                    "_audit_role": "developer",
                }
            )

        already_in_history = _current_user_message_already_in_history(
            history_messages,
            user_message=user_message,
            user_message_role=user_message_role,
            user_attachments=user_attachments,
        )

        if not skip_user_message and not already_in_history:
            if routing_reminder:
                messages.append(
                    {
                        "role": "system",
                        "content": routing_reminder,
                        "_routing_reminder": True,
                        "_audit_source": "routing_reminder",
                        "_audit_role": "developer",
                    }
                )
            if attachment_notice:
                messages.append(
                    {
                        "role": "system",
                        "content": attachment_notice,
                        "_audit_source": "attachment_notice",
                        "_audit_role": "developer",
                    }
                )
            if attachment_context:
                messages.append(
                    {
                        "role": "user",
                        "content": attachment_context,
                        "_audit_source": "attachment_notice",
                        "_audit_role": "user",
                    }
                )
            current_turn_message = _current_turn_attachment_message(
                user_message=user_message,
                user_message_role=user_message_role,
                user_attachments=user_attachments,
                model_info=model_info,
                include_user_message=True,
            )
            if current_turn_message is not None:
                messages.append(current_turn_message)
        elif already_in_history:
            if routing_reminder:
                messages.append(
                    {
                        "role": "system",
                        "content": routing_reminder,
                        "_routing_reminder": True,
                        "_audit_source": "routing_reminder",
                        "_audit_role": "developer",
                    }
                )
            if attachment_notice:
                messages.append(
                    {
                        "role": "system",
                        "content": attachment_notice,
                        "_audit_source": "attachment_notice",
                        "_audit_role": "developer",
                    }
                )
            if attachment_context:
                messages.append(
                    {
                        "role": "user",
                        "content": attachment_context,
                        "_audit_source": "attachment_notice",
                        "_audit_role": "user",
                    }
                )
            current_turn_attachments = _current_turn_attachment_message(
                user_message=user_message,
                user_message_role=user_message_role,
                user_attachments=user_attachments,
                model_info=model_info,
                include_user_message=False,
            )
            if current_turn_attachments is not None:
                messages.append(current_turn_attachments)

        projection = project_messages(messages)
        messages = self._prune_messages(
            messages=projection.messages,
            resolved_model=resolved_model,
            max_prompt_tokens=max_prompt_tokens,
            tool_schema_tokens=tool_schema_tokens,
        )

        audit_messages = self._collect_audit_messages(messages)
        cache_breakpoint_index = _find_cache_breakpoint(messages)

        for msg in messages:
            msg.pop("_immutable_prefix", None)
            msg.pop("_project_context", None)
            msg.pop("_prior_context", None)
            msg.pop("_follow_up_context", None)
            msg.pop("_routing_reminder", None)
            msg.pop("_audit_source", None)
            msg.pop("_audit_role", None)
        prompt_tokens = (
            self.llm.count_messages_tokens(messages, resolved_model) + tool_schema_tokens
        )
        recommend_compaction = (
            budget.available_prompt_tokens > 0
            and (prompt_tokens / budget.available_prompt_tokens) >= self.compaction_threshold
        )
        logger.info(
            "context: assembly completed (skip_memory)",
            extra={
                "extra_data": {
                    "session_id": session.session_id,
                    "prompt_tokens": prompt_tokens,
                    "recommend_compaction": recommend_compaction,
                }
            },
        )
        return ContextAssemblyResult(
            messages=messages,
            audit_messages=audit_messages,
            degraded=bool(degraded_sources),
            degraded_sources=sorted(set(degraded_sources)),
            resolved_model=resolved_model,
            static_tokens=static_tokens,
            dynamic_tokens=dynamic_tokens,
            prompt_tokens=prompt_tokens,
            max_context_tokens=max_context_tokens,
            recommend_compaction=recommend_compaction,
            cache_breakpoint_index=cache_breakpoint_index,
        )

    def _get_available_skills_metadata(self, agent: AgentDefinition) -> str | None:
        """Get compact available-skills metadata for the immutable prompt prefix.

        Returns pre-built XML metadata if set by the runtime assembly layer,
        or ``None`` if no skills are available.  This is intentionally
        token-light — full instructions are loaded via ``skill_load``.
        """
        if not isinstance(agent.skills, dict):
            return None

        # Check for pre-built metadata from runtime assembly
        metadata = agent.skills.get("_available_skills_metadata")
        if isinstance(metadata, str) and metadata.strip():
            return metadata
        return None

    async def _ensure_immutable_prefix(
        self,
        *,
        session: SessionModel,
        agent: AgentDefinition,
        project_instructions: list[str],
        memory_labels: dict[str, str],
        context: str | None,
        allow_empty_memory: bool = False,
    ) -> list[ImmutablePrefixEntry]:
        cached_entries = self.session_cache.get_prefix_entries(session.session_id)
        if cached_entries and not self.session_cache.needs_prefix_repair(session.session_id):
            return cached_entries

        repair_needed = self.session_cache.needs_prefix_repair(session.session_id)
        snapshot_source = "repair" if repair_needed else "bootstrap"
        if snapshot_source == "repair":
            last_attempt = self.session_cache.get_last_repair_attempt_at(session.session_id)
            if last_attempt is not None and (monotonic() - last_attempt) < 300.0:
                raise ImmutablePrefixUnavailable(
                    "Immutable prefix repair is cooling down.",
                    reason="cooldown",
                )

        try:
            instructions: str | None = None
            core_memories: str | None = None
            repair_reason: str | None = None
            requested_session_id = session.mnemory_session_id
            previous_mnemory_session_id = session.mnemory_session_id
            recall_session_id = session.mnemory_session_id
            if not allow_empty_memory:
                identity_payload = await self._load_identity_payload(
                    session_id=session.mnemory_session_id,
                    session=session,
                    memory_labels=memory_labels,
                    context=context,
                )
                raw_instructions = identity_payload.get("instructions")
                raw_core = identity_payload.get("core_memories")
                recall_session_id = (
                    str(identity_payload.get("session_id") or "").strip() or recall_session_id
                )
                if (
                    requested_session_id
                    and recall_session_id
                    and recall_session_id != requested_session_id
                ):
                    repair_reason = "mnemory_session_forged"
                    logger.warning(
                        "context: adopting forged Mnemory session while rebuilding immutable prefix",
                        extra={
                            "extra_data": {
                                "session_id": session.session_id,
                                "old_mnemory_session_id": requested_session_id,
                                "new_mnemory_session_id": recall_session_id,
                            }
                        },
                    )
                    await self._adopt_mnemory_session(session, recall_session_id)
                elif session.mnemory_session_id is None and recall_session_id:
                    await self._adopt_mnemory_session(session, recall_session_id)

                if isinstance(raw_instructions, str) and raw_instructions.strip():
                    instructions = _adapt_memory_instructions(raw_instructions.strip())
                if isinstance(raw_core, str) and raw_core.strip():
                    core_memories = raw_core.strip()

                if not core_memories and requested_session_id is not None:
                    repair_reason = repair_reason or "existing_session_returned_no_core"
                    logger.warning(
                        "context: existing Mnemory session could not resume immutable prefix, creating fresh repair session",
                        extra={
                            "extra_data": {
                                "session_id": session.session_id,
                                "old_mnemory_session_id": requested_session_id,
                                "reason": repair_reason,
                            }
                        },
                    )
                    identity_payload = await self._load_identity_payload(
                        session_id=None,
                        session=session,
                        memory_labels=memory_labels,
                        context=context,
                    )
                    raw_instructions = identity_payload.get("instructions")
                    raw_core = identity_payload.get("core_memories")
                    recall_session_id = str(identity_payload.get("session_id") or "").strip()
                    if recall_session_id:
                        await self._adopt_mnemory_session(session, recall_session_id)
                    instructions = (
                        _adapt_memory_instructions(raw_instructions.strip())
                        if isinstance(raw_instructions, str) and raw_instructions.strip()
                        else None
                    )
                    core_memories = (
                        raw_core.strip() if isinstance(raw_core, str) and raw_core.strip() else None
                    )

                if not core_memories:
                    raise ImmutablePrefixUnavailable(
                        "Core memories are unavailable for this session.",
                        reason="missing_core",
                    )

            prefix_entries = self._compose_prefix_entries(
                agent=agent,
                project_instructions=project_instructions,
                memory_instructions=instructions,
                core_memories=core_memories,
                compaction_summary=self.session_cache.get_compaction_summary(session.session_id),
            )
            if not prefix_entries:
                return []

            intaris_session_id = session.intaris_session_id or session.session_id
            message_events = build_prefix_message_events(prefix_entries)
            idempotency_key = f"{intaris_session_id}:immutable_prefix:{snapshot_source}:messages"
            with scoped_runtime_context(user_email=session.user_email, agent_id=session.agent_id):
                append_result = await self.guardrails.record_events(
                    session_id=intaris_session_id,
                    events=message_events,
                    source="cognis",
                    idempotency_key=idempotency_key,
                )
            if not append_result.ok:
                raise ImmutablePrefixUnavailable(
                    "Immutable prefix could not be persisted.",
                    reason="record_failed",
                )

            resolved_entries = [
                ImmutablePrefixEntry(
                    role=entry.role,
                    source=entry.source,
                    content=entry.content,
                    seq=append_result.first_seq + index,
                )
                for index, entry in enumerate(sort_prefix_entries(prefix_entries))
            ]
            snapshot_event = build_context_snapshot_event(
                resolved_entries,
                snapshot_source=snapshot_source,
                extras={
                    "mnemory_session_id": recall_session_id,
                    "agent_id": agent.agent_id,
                    "old_mnemory_session_id": previous_mnemory_session_id,
                    "repair_reason": repair_reason,
                },
            )
            with scoped_runtime_context(user_email=session.user_email, agent_id=session.agent_id):
                snapshot_result = await self.guardrails.record_events(
                    session_id=intaris_session_id,
                    events=[snapshot_event],
                    source="cognis",
                    idempotency_key=f"{intaris_session_id}:immutable_prefix:{snapshot_source}:snapshot",
                )
            if not snapshot_result.ok:
                raise ImmutablePrefixUnavailable(
                    "Immutable prefix snapshot could not be persisted.",
                    reason="record_failed",
                )
            await self.session_cache.append_recorded_events(session, message_events, append_result)
            await self.session_cache.append_recorded_events(
                session, [snapshot_event], snapshot_result
            )
            await self.session_cache.store_prefix_snapshot(
                session.session_id,
                resolved_entries,
                snapshot_seq=snapshot_result.last_seq,
                snapshot_source=snapshot_source,
            )
            if snapshot_source == "repair":
                MNEMORY_SESSION_REPAIRED_TOTAL.labels(
                    reason=repair_reason or "intaris_snapshot_missing"
                ).inc()
            return resolved_entries
        except Exception:
            if snapshot_source == "repair":
                await self.session_cache.note_repair_attempt(session.session_id)
            raise

    async def _load_identity_payload(
        self,
        *,
        session_id: str | None,
        session: SessionModel,
        memory_labels: dict[str, str],
        context: str | None,
    ) -> dict[str, Any]:
        with scoped_runtime_context(user_email=session.user_email, agent_id=session.agent_id):
            return await self.memory.load_session_identity(
                session_id=session_id,
                labels=memory_labels,
                context=context,
            )

    async def _adopt_mnemory_session(
        self,
        session: SessionModel,
        mnemory_session_id: str,
    ) -> bool:
        if not mnemory_session_id or session.mnemory_session_id == mnemory_session_id:
            return False
        updated = await self.session_manager.attach_mnemory_session(
            session.session_id,
            mnemory_session_id,
        )
        if updated:
            session.mnemory_session_id = mnemory_session_id
        return updated

    def _compose_prefix_entries(
        self,
        *,
        agent: AgentDefinition,
        project_instructions: list[str],
        memory_instructions: str | None,
        core_memories: str | None,
        compaction_summary: str | None,
    ) -> list[ImmutablePrefixEntry]:
        entries: list[ImmutablePrefixEntry] = []
        identity_prompt = _compose_identity_prompt(agent)
        if identity_prompt:
            entries.append(
                ImmutablePrefixEntry(role="system", source="identity", content=identity_prompt)
            )
        if project_instructions:
            entries.append(
                ImmutablePrefixEntry(
                    role="developer",
                    source="project_instructions",
                    content="\n\n".join(project_instructions),
                )
            )
        if memory_instructions:
            entries.append(
                ImmutablePrefixEntry(
                    role="developer",
                    source="memory_instructions",
                    content=memory_instructions,
                )
            )
        if core_memories:
            entries.append(
                ImmutablePrefixEntry(
                    role="developer",
                    source="core_memories",
                    content=core_memories,
                )
            )
        if compaction_summary:
            entries.append(
                ImmutablePrefixEntry(
                    role="developer",
                    source="compaction_summary",
                    content=compaction_summary,
                )
            )
        return sort_prefix_entries(entries)

    @staticmethod
    def _prefix_content(
        prefix_entries: list[ImmutablePrefixEntry],
        source: str,
    ) -> str | None:
        for entry in sort_prefix_entries(prefix_entries):
            if entry.source == source:
                return entry.content
        return None

    @staticmethod
    def _collect_audit_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        audit_messages: list[dict[str, Any]] = []
        for index, message in enumerate(messages):
            audit_source = message.get("_audit_source")
            content = message.get("content")
            if not isinstance(audit_source, str) or not isinstance(content, str):
                continue
            role = str(message.get("_audit_role") or message.get("role") or "system")
            audit_messages.append(
                {
                    "position": index,
                    "role": role,
                    "content": content,
                    "source": audit_source,
                    "content_type": "text",
                    "hash": _audit_hash(role, content, audit_source),
                }
            )
        return audit_messages

    def _project_context_messages(self, session_id: str) -> list[dict[str, Any]]:
        get_project_contexts = getattr(self.session_cache, "get_project_contexts", None)
        if not callable(get_project_contexts):
            return []
        messages: list[dict[str, Any]] = []
        for entry in get_project_contexts(session_id):
            if not getattr(entry, "content", None):
                continue
            messages.append(
                {
                    "role": "system",
                    "content": str(entry.content),
                    "_project_context": True,
                }
            )
        return messages

    def _compose_immutable_prefix(
        self,
        *,
        agent: AgentDefinition,
        prompt_context: PromptContext,
        prefix_entries: list[ImmutablePrefixEntry],
        resolved_model: str,
    ) -> str | None:
        """Compose the cacheable immutable system prefix as one message."""
        sections: list[str] = []

        identity_prompt = self._prefix_content(prefix_entries, "identity")
        immutable_instructions = self._prefix_content(prefix_entries, "memory_instructions")
        immutable_core_memories = self._prefix_content(prefix_entries, "core_memories")
        compaction_summary = self._prefix_content(prefix_entries, "compaction_summary")

        if identity_prompt:
            tagged_identity = _tagged_section("identity", identity_prompt)
            if tagged_identity:
                sections.append(tagged_identity)

        critical_rules = build_critical_rules(agent_id=agent.agent_id)
        if critical_rules:
            tagged_rules = _tagged_section("critical_rules", critical_rules)
            if tagged_rules:
                sections.append(tagged_rules)

        include_work_routing = True
        if agent.permissions is not None:
            include_work_routing = (
                agent.permissions.resolve_permission("delegate", tool_id="delegate")
                is not Permission.DENY
            )
        system_instructions = build_system_instructions(
            prompt_context,
            agent_id=agent.agent_id,
            include_work_routing=include_work_routing,
        )
        if system_instructions:
            tagged_instructions = _tagged_section("instructions", system_instructions)
            if tagged_instructions:
                sections.append(tagged_instructions)

        if immutable_instructions:
            sections.append(
                f"<memory_instructions>\n{self._cap_prefix_section(immutable_instructions, resolved_model, 4000)}\n</memory_instructions>"
            )

        if immutable_core_memories:
            sections.append(
                '<memory_context trust="untrusted">\n'
                + self._cap_prefix_section(immutable_core_memories, resolved_model, 4000)
                + "\n</memory_context>"
            )

        skill_metadata = self._get_available_skills_metadata(agent)
        if skill_metadata:
            sections.append(skill_metadata)
            tagged_skills_guidance = _tagged_section(
                "skills_guidance",
                "You have skills that extend your capabilities. Review the "
                "list above and use skill_load to load any skills relevant "
                "to the current task. Skills marked as attached are preferred "
                "defaults for this agent. Follow loaded skill instructions "
                "carefully. You can also create new skills with skill_write "
                "to remember procedures for future use.",
            )
            if tagged_skills_guidance:
                sections.append(tagged_skills_guidance)

        compaction_block = _format_compaction_summary(compaction_summary)
        if compaction_block:
            tagged_summary = _tagged_section("continuation_summary", compaction_block)
            if tagged_summary:
                sections.append(tagged_summary)

        return "\n\n".join(section for section in sections if section) or None

    def _cap_prefix_section(self, text: str, resolved_model: str, max_tokens: int) -> str:
        if not text:
            return text
        try:
            if self.llm.count_tokens(text, resolved_model) <= max_tokens:
                return text
        except Exception:
            return text[: max_tokens * 4]

        low = 0
        high = len(text)
        best = text[: max_tokens * 4]
        while low <= high:
            mid = (low + high) // 2
            candidate = text[:mid]
            try:
                tokens = self.llm.count_tokens(candidate, resolved_model)
            except Exception:
                return best
            if tokens <= max_tokens:
                best = candidate
                low = mid + 1
            else:
                high = mid - 1
        return best.rstrip() + "\n[truncated to fit immutable prefix budget]"

    def _count_static_tokens(
        self,
        *,
        resolved_model: str,
        immutable_prefix: str | None,
        tool_definitions: list[ToolDefinition],
    ) -> tuple[int, int]:
        system_prompt_tokens = 0
        tool_schema_tokens = 0
        if immutable_prefix:
            system_prompt_tokens += self.llm.count_tokens(immutable_prefix, resolved_model)
        if tool_definitions:
            schemas = json.dumps(
                [tool.model_dump(mode="json") for tool in tool_definitions],
                sort_keys=True,
            )
            tool_schema_tokens += self.llm.count_tokens(schemas, resolved_model)
        return system_prompt_tokens, tool_schema_tokens

    def _events_to_messages(self, events: list[Any]) -> list[dict[str, Any]]:
        return events_to_messages(events)

    def _prune_messages(
        self,
        *,
        messages: list[dict[str, Any]],
        resolved_model: str,
        max_prompt_tokens: int,
        tool_schema_tokens: int,
    ) -> list[dict[str, Any]]:
        if max_prompt_tokens <= 0:
            return messages

        pruned_messages = list(messages)
        while (
            self.llm.count_messages_tokens(pruned_messages, resolved_model) + tool_schema_tokens
            > max_prompt_tokens
        ):
            # Priority 1: Drop mutable recalled memories (search results)
            recalled_index = next(
                (
                    index
                    for index, message in enumerate(pruned_messages)
                    if message.get("role") == "system"
                    and isinstance(message.get("content"), str)
                    and "Recalled memories:" in str(message.get("content"))
                    and '<memory_context trust="untrusted">' in str(message.get("content"))
                ),
                None,
            )
            if recalled_index is not None:
                pruned_messages.pop(recalled_index)
                continue

            # Priority 2: Drop oldest non-protected messages (history).
            # Tool call groups (assistant message with tool_calls + matching
            # tool role responses) must be dropped atomically to avoid
            # orphaned tool_calls that LLM providers reject.
            indices_to_drop = _find_oldest_droppable_group(pruned_messages)
            if not indices_to_drop:
                break
            for idx in sorted(indices_to_drop, reverse=True):
                pruned_messages.pop(idx)
        return pruned_messages


def events_to_messages(events: list[Any]) -> list[dict[str, Any]]:
    """Convert session events to LLM message dicts.

    This is the canonical event-to-message formatter used by both the
    ``ContextAssembler`` and the ``StepContextAssembler``.  It supports
    both ``CachedEvent`` objects (with ``.type`` / ``.data`` attributes)
    and raw ``dict`` events from Intaris reads.

    Tool calls are reconstructed in the proper OpenAI function-calling
    format: consecutive ``tool_call`` events are grouped into a single
    assistant message with a ``tool_calls`` array, and ``tool_result``
    events become ``role: "tool"`` messages with matching ``tool_call_id``.
    When an ``assistant_message`` immediately precedes tool calls (same
    LLM response), the tool_calls array is merged onto that message.
    """
    messages: list[dict[str, Any]] = []
    # Buffer for consecutive tool_call events (flushed on non-tool_call)
    pending_tool_calls: list[dict[str, Any]] = []
    open_tool_call_ids: list[str] = []

    def _flush_tool_calls() -> None:
        """Flush buffered tool_call events into an assistant message."""
        if not pending_tool_calls:
            return
        tc_array = list(pending_tool_calls)
        pending_tool_calls.clear()
        open_tool_call_ids.extend(
            tc.get("id", "") for tc in tc_array if isinstance(tc.get("id", ""), str)
        )
        # Merge onto preceding assistant message if it has no tool_calls yet
        if (
            messages
            and messages[-1].get("role") == "assistant"
            and "tool_calls" not in messages[-1]
        ):
            messages[-1]["tool_calls"] = tc_array
            # OpenAI requires content to be null (not absent) when tool_calls present
            if not messages[-1].get("content"):
                messages[-1]["content"] = None
        else:
            messages.append({"role": "assistant", "content": None, "tool_calls": tc_array})

    def _append_orphan_placeholders() -> None:
        """Close unresolved tool calls with synthetic tool messages."""

        while open_tool_call_ids:
            tc_id = open_tool_call_ids.pop(0)
            if not tc_id:
                continue
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tc_id,
                    "content": "[No result recorded - step may have been interrupted]",
                }
            )

    def _assistant_attachment_context(attachments: list[dict[str, Any]]) -> str:
        lines: list[str] = []
        for attachment in attachments:
            label = _attachment_label(attachment).replace("\r", " ").replace("\n", " ")
            lines.append(f"- {html.escape(label, quote=True)}")
        return "<assistant_attachments>\n" + "\n".join(lines) + "\n</assistant_attachments>"

    for event in events:
        if isinstance(event, dict):
            event_type = str(event.get("type", ""))
            event_data: dict[str, Any] = event.get("data", {})
        else:
            event_type = event.type
            event_data = event.data

        if event_type == "tool_call":
            tool_name = event_data.get("tool_name") or event_data.get("name")
            call_id = event_data.get("call_id", "")
            arguments = event_data.get("arguments")
            if isinstance(tool_name, str):
                # Serialize arguments to JSON string as required by the format
                if isinstance(arguments, dict):
                    args_str = json.dumps(arguments, default=str)
                elif isinstance(arguments, str):
                    args_str = arguments
                else:
                    args_str = "{}"
                pending_tool_calls.append(
                    {
                        "id": call_id,
                        "type": "function",
                        "function": {"name": tool_name, "arguments": args_str},
                    }
                )
            continue

        # Any non-tool_call event flushes the pending buffer
        _flush_tool_calls()

        if event_type == "user_message":
            _append_orphan_placeholders()
            content = event_data.get("content")
            if isinstance(content, str):
                attachments = event_data.get("attachments")
                if isinstance(attachments, list) and attachments:
                    content = f"{content}\n\n{_attachment_note([a for a in attachments if isinstance(a, dict)])}"
                messages.append({"role": "user", "content": content})
        elif event_type == "assistant_message":
            _append_orphan_placeholders()
            content = event_data.get("content")
            if isinstance(content, str):
                attachments = event_data.get("attachments")
                if isinstance(attachments, list) and attachments:
                    content = f"{content}\n\n{_assistant_attachment_context([a for a in attachments if isinstance(a, dict)])}"
                messages.append({"role": "assistant", "content": content})
        elif event_type == "tool_result":
            # The agent loop stores tool output under key "result";
            # fall back to "output" for forward-compatibility.
            output = event_data.get("result") or event_data.get("output")
            call_id = event_data.get("call_id", "")
            if isinstance(output, str) and call_id in open_tool_call_ids:
                open_tool_call_ids.remove(call_id)
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call_id,
                        "content": output,
                        "_tool_name": event_data.get("name"),
                        "_protected_tool_output": bool(event_data.get("protect_from_pruning")),
                        "_has_full_output": bool(event_data.get("has_full_output")),
                        "_recovery_call_id": event_data.get("recovery_call_id"),
                        "_output_size": event_data.get("output_size"),
                    }
                )
        elif event_type == "delegation":
            _append_orphan_placeholders()
            status = event_data.get("status")
            if status == "completed":
                child_id = event_data.get("child_session_id", "unknown")
                mode = event_data.get("mode", "delegate")
                # Prefer full content over summary for delegation results
                result_content = event_data.get("result_content", "")
                result_summary = event_data.get("result_summary", "No result provided.")
                result_text = result_content if result_content else result_summary
                messages.append(
                    {
                        "role": "system",
                        "content": (
                            f'<delegation_result session="{child_id}" mode="{mode}" status="completed">\n'
                            f"{result_text}\n"
                            f"</delegation_result>"
                        ),
                    }
                )
            elif status == "failed":
                child_id = event_data.get("child_session_id", "unknown")
                mode = event_data.get("mode", "delegate")
                error = event_data.get("error", "Unknown error")
                messages.append(
                    {
                        "role": "system",
                        "content": (
                            f'<delegation_result session="{child_id}" mode="{mode}" status="failed">\n'
                            f"Error: {error}\n"
                            f"</delegation_result>"
                        ),
                    }
                )
            else:
                # Initial delegation (no status or status="started")
                messages.append(
                    {
                        "role": "system",
                        "content": _format_delegation_status(event_data),
                    }
                )
        elif event_type == "lifecycle":
            _append_orphan_placeholders()
            lifecycle_event = event_data.get("event", "")
            if lifecycle_event in {"task_result", "task_failed", "task_cancelled"}:
                messages.append(
                    {
                        "role": "system",
                        "content": _format_task_update(lifecycle_event, event_data),
                    }
                )
            elif lifecycle_event == "step_complete":
                summary = event_data.get("summary", "")
                if summary:
                    messages.append(
                        {
                            "role": "system",
                            "content": f"Step completed: {summary}",
                        }
                    )
            elif lifecycle_event == "system_notice":
                notice_msg = event_data.get("message", "")
                if notice_msg:
                    messages.append({"role": "system", "content": notice_msg})
            # Other lifecycle events (task_status, etc.) are informational — skip
        elif event_type == "evaluation":
            _append_orphan_placeholders()
            eval_event = event_data.get("event", "")
            if eval_event == "evaluation_feedback":
                attempt = event_data.get("attempt", "?")
                decision = event_data.get("decision", "revise")
                feedback = event_data.get("feedback", "")
                messages.append(
                    {
                        "role": "system",
                        "content": (
                            f'<evaluation_feedback attempt="{attempt}">\n'
                            f"Decision: {decision}\n"
                            f"Feedback: {feedback}\n"
                            f"</evaluation_feedback>"
                        ),
                    }
                )
    # Flush any remaining buffered tool calls at the end of the event list.
    # If these are orphaned (no matching tool_result events follow), they
    # would create an invalid message sequence that LLM providers reject.
    # We still flush them but add synthetic placeholder tool results so
    # the message sequence is valid.
    if pending_tool_calls:
        _flush_tool_calls()
    _append_orphan_placeholders()
    return messages


def _current_user_message_already_in_history(
    history_messages: list[dict[str, Any]],
    *,
    user_message: str,
    user_message_role: str,
    user_attachments: list[dict[str, Any]] | None,
) -> bool:
    """Return True when ``user_message`` is already the trailing user entry.

    The controller records the current turn's user message into the Intaris
    event store before assembling context so the intention barrier can start
    updating in parallel. That record is then replayed as part of
    ``history_messages``. Without deduping, ``assemble()`` would append the
    same message a second time and the LLM would see the current prompt
    twice — a harness protocol defect that destabilizes model behavior
    (observed on gpt-5.4 with low reasoning emitting pathological no-op tool
    calls and duplicated final outputs).

    We only consider it a duplicate when:

    * ``user_message_role == "user"`` (system-initiated turns do not record
      a ``user_message`` event),
    * the last message in history is a ``role: "user"`` string-content
      message,
    * and the textual content matches (attachment note variants included).

    Returns ``False`` for any uncertainty so the original, safe behavior —
    appending the current message explicitly — is preserved. Missing
    user_message still yields ``False`` so we do not prevent a legitimate
    empty user_message when user_message_role is "user".
    """

    if user_message_role != "user":
        return False
    if not history_messages:
        return False

    # Walk backwards, skipping tool/assistant continuations to find the
    # last genuine user-role message.
    last_user_content: str | None = None
    for message in reversed(history_messages):
        if message.get("role") != "user":
            # If we hit any non-user role (assistant with tool_calls,
            # tool results, system), the most recent user turn already
            # has an assistant response or aux content after it, which
            # means this turn's prompt has not been replayed as the tail.
            return False
        content = message.get("content")
        if isinstance(content, str):
            last_user_content = content
            break
        if isinstance(content, list):
            # Attachment-form message: concatenate text blocks.
            text_parts = [
                part.get("text", "")
                for part in content
                if isinstance(part, dict) and part.get("type") == "text"
            ]
            last_user_content = "".join(text_parts)
            break
        return False

    if last_user_content is None:
        return False

    normalized_history = last_user_content.strip()
    if not normalized_history:
        return False

    # Exact match (no attachment note appended during the original
    # recording).
    if normalized_history == user_message.strip():
        return True

    # Match when an attachment note was appended during recording
    # (_user_message_for_recording may add one).
    if user_message and normalized_history.startswith(user_message.strip()):
        suffix = normalized_history[len(user_message.strip()) :].lstrip()
        # Accept the append-only attachment-note pattern; do not risk a
        # false positive on unrelated suffixes.
        if not suffix:
            return True
        if _looks_like_attachment_note_suffix(suffix):
            return True

    return False


def _looks_like_attachment_note_suffix(text: str) -> bool:
    """Heuristic: trailing text looks like an attachment note artifact.

    We only use this to decide whether a recorded user message is the same
    as the current one with an attachment note appended during history
    replay. The canonical format produced by ``attachment_note`` is
    ``"Attachments: <name> (<kind>, artifact_id=<id>), ..."`` (see
    ``cognis/core/attachment_utils.py``). A few legacy/synthetic variants
    are also accepted to keep the dedupe robust if the recording format
    changes slightly.
    """

    if not text:
        return False
    stripped = text.lstrip()
    return stripped.startswith(
        (
            "Attachments:",
            "Attached files",
            "<attachments",
            "Attachment:",
        )
    )


def _find_cache_breakpoint(messages: list[dict[str, Any]]) -> int | None:
    """Find the index of the last immutable prefix message.

    The cache breakpoint is the last system message in the consolidated
    immutable prefix. Everything after this index is mutable and changes
    every turn.
    """

    if not messages:
        return None

    last_immutable = None
    for index, message in enumerate(messages):
        if _is_immutable_prefix_message(message):
            last_immutable = index
        elif message.get("role") != "system" or last_immutable is not None:
            break
    return last_immutable


def _format_search_results(search_results: Any) -> str | None:
    """Format per-turn recalled memories (mutable part of memory context)."""

    if not isinstance(search_results, list) or not search_results:
        return None
    lines: list[str] = []
    for result in search_results:
        if not isinstance(result, dict):
            continue
        memory = result.get("memory")
        if not isinstance(memory, str) or not memory.strip():
            continue
        score = result.get("score")
        prefix = f"- ({score:.2f}) " if isinstance(score, (int, float)) else "- "
        lines.append(prefix + memory.strip())
    return "\n".join(lines) if lines else None


def _find_oldest_droppable_group(
    messages: list[dict[str, Any]],
) -> list[int]:
    """Find the indices of the oldest droppable message group.

    Tool call groups (an assistant message with ``tool_calls`` followed by
    matching ``tool`` role responses) are treated atomically — all indices
    in the group are returned so they can be dropped together.  Standalone
    messages return a single-element list.

    Returns an empty list when no droppable messages remain.
    """
    last_idx = len(messages) - 1
    for i, msg in enumerate(messages):
        if i == last_idx:
            continue  # Never drop the last message (current user turn)
        if _is_protected_context_message(msg):
            continue
        if msg.get("_follow_up_context"):
            continue

        # If this is an assistant message with tool_calls, collect the
        # entire group (assistant + all matching tool responses).
        # Never include the last message (current user turn) in a group.
        if msg.get("role") == "assistant" and msg.get("tool_calls"):
            call_ids = {tc.get("id") or tc.get("call_id", "") for tc in msg["tool_calls"]} - {""}
            group = [i]
            for j in range(i + 1, last_idx):
                if (
                    messages[j].get("role") == "tool"
                    and messages[j].get("tool_call_id") in call_ids
                ):
                    group.append(j)
            return group

        # If this is a tool response, find its parent assistant message
        # and drop the entire group from the parent.
        if msg.get("role") == "tool":
            target_id = msg.get("tool_call_id", "")
            if not target_id:
                return [i]  # Malformed — safe to drop individually
            for k in range(i - 1, -1, -1):
                parent = messages[k]
                if parent.get("role") == "assistant" and parent.get("tool_calls"):
                    parent_ids = {
                        tc.get("id") or tc.get("call_id", "") for tc in parent["tool_calls"]
                    } - {""}
                    if target_id in parent_ids:
                        group = [k]
                        for j in range(k + 1, last_idx):
                            if (
                                messages[j].get("role") == "tool"
                                and messages[j].get("tool_call_id") in parent_ids
                            ):
                                group.append(j)
                        return group

        # If this is a tool response, find its parent assistant message
        # and drop the entire group from the parent.
        if msg.get("role") == "tool":
            target_id = msg.get("tool_call_id", "")
            for k in range(i - 1, -1, -1):
                parent = messages[k]
                if parent.get("role") == "assistant" and parent.get("tool_calls"):
                    parent_ids = {
                        tc.get("id") or tc.get("call_id", "") for tc in parent["tool_calls"]
                    }
                    if target_id in parent_ids:
                        group = [k]
                        for j in range(k + 1, len(messages)):
                            if (
                                messages[j].get("role") == "tool"
                                and messages[j].get("tool_call_id") in parent_ids
                            ):
                                group.append(j)
                        return group
            # Orphaned tool response — safe to drop individually
            return [i]

        # Standalone message (user, assistant without tool_calls, system)
        return [i]

    return []


def _is_immutable_prefix_message(message: dict[str, Any]) -> bool:
    """Check if a message belongs to the cacheable immutable prompt prefix.

    The assembled immutable prefix is emitted as a single marked system
    message so cache-breakpoint detection does not need content heuristics.
    """
    return message.get("role") == "system" and bool(message.get("_immutable_prefix"))


def _is_protected_context_message(message: dict[str, Any]) -> bool:
    """Check if a message is protected from pruning.

    Protected messages that should never be pruned:
    - Consolidated immutable prefix
    - Frozen project instruction messages loaded during the session
    - Prior step context (workflow step output from a previous step)
    """
    if message.get("_prior_context"):
        return True
    if message.get("_project_context"):
        return True
    return _is_immutable_prefix_message(message)


# ---------------------------------------------------------------------------
# Instruction adaptation: MCP tool names → Cognis builtin tool names
# ---------------------------------------------------------------------------

_MCP_MEMORY_TOOL_ALIASES: tuple[tuple[str, str], ...] = (
    ("get_recent_memories", "memory_recent"),
    ("search_memories", "memory_search"),
    ("find_memories", "memory_find"),
    ("ask_memories", "memory_ask"),
    ("add_memories", "memory_add_batch"),
    ("add_memory", "memory_add"),
    ("update_memory", "memory_update"),
    ("delete_memory", "memory_delete"),
    ("list_memories", "memory_list"),
    ("list_categories", "memory_categories"),
    ("get_artifact_url", "memory_get_artifact_url"),
    ("save_artifact", "memory_save_artifact"),
    ("delete_artifact", "memory_delete_artifact"),
    ("list_artifacts", "memory_list_artifacts"),
    ("get_artifact", "memory_get_artifact"),
)


def _adapt_memory_instructions(instructions: str) -> str:
    """Adapt mnemory instructions for Cognis builtin tool names.

    Replaces MCP-style tool names (search_memories, add_memory, etc.) with
    Cognis builtin names (memory_search, memory_add, etc.) so the LLM can
    connect the behavioral guidance to the actual tools in its schema.

    Also annotates references to tools that don't exist in Cognis
    (initialize_memory, get_core_memories) with "(not available)".
    """
    from cognis.tools.builtin.memory import MEMORY_TOOL_NAMES

    text = instructions
    available_aliases = [
        (mcp_name, cognis_name)
        for mcp_name, cognis_name in _MCP_MEMORY_TOOL_ALIASES
        if cognis_name in MEMORY_TOOL_NAMES
    ]
    available_names = {name for name, _ in available_aliases}
    known_cognis_names = set(MEMORY_TOOL_NAMES)
    candidate_names = {
        match.group(0)
        for match in re.finditer(r"\b[a-z][a-z0-9_]{2,}\b", instructions)
        if match.group(0).startswith(
            (
                "get_",
                "search_",
                "find_",
                "ask_",
                "add_",
                "update_",
                "delete_",
                "list_",
                "save_",
                "initialize_",
            )
        )
    }
    unavailable_names = sorted(
        name
        for name in candidate_names
        if name not in available_names and name not in known_cognis_names
    )
    for name in unavailable_names:
        pattern = re.compile(rf"\b{re.escape(name)}\b")
        text = pattern.sub(f"{name} (not available)", text)
    for mcp_name, cognis_name in available_aliases:
        pattern = re.compile(rf"\b{re.escape(mcp_name)}\b")
        text = pattern.sub(cognis_name, text)

    return text


def _format_delegation_status(data: dict[str, Any]) -> str:
    mode = data.get("mode", "delegate")
    task = data.get("task", "")
    child_session_id = data.get("child_session_id", "")
    parts = [f"[Delegated ({mode})"]
    if task:
        parts.append(f": {task}")
    parts.append("]")
    if child_session_id:
        parts.append(f" session={child_session_id}")
    return "".join(parts)


def _format_active_delegations(active_delegations: list[dict[str, Any]]) -> str:
    lines = ["Active delegations:"]
    for delegation in active_delegations:
        child_session_id = delegation.get("child_session_id", "unknown")
        status = delegation.get("status", "unknown")
        task = delegation.get("task")
        line = f"- {child_session_id}: {status}"
        if isinstance(task, str) and task:
            line = f"{line} ({task})"
        lines.append(line)
    return "\n".join(lines)


def _format_task_update(lifecycle_event: str, data: dict[str, Any]) -> str:
    title = data.get("title") or data.get("task_title") or data.get("task_id") or "Background task"
    result_summary = data.get("result_summary") or "No summary provided."
    status = data.get("status") or {
        "task_result": "completed",
        "task_failed": "failed",
        "task_cancelled": "cancelled",
    }.get(lifecycle_event, "updated")
    return f"Task update: {title} {status}. Summary: {result_summary}"
