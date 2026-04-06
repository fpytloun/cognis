"""Context assembly for chat turns."""

from __future__ import annotations

import asyncio
import datetime
import json
import re
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from cognis.core.prompts import PromptContext, build_system_instructions
from cognis.core.runtime import ExecutorEnvironmentSnapshot, build_local_executor_environment
from cognis.logging import get_logger
from cognis.models.agent import AgentDefinition
from cognis.models.artifact import ArtifactKind
from cognis.models.session import ConversationModel, SessionModel
from cognis.models.tool import ToolDefinition
from cognis.runtime_context import scoped_runtime_context
from cognis.store.queries import get_setting_value, update_conversation

logger = get_logger(__name__)

EVENT_TYPES_FOR_CONTEXT = [
    "user_message",
    "assistant_message",
    "tool_call",
    "tool_result",
    "delegation",
    "lifecycle",
    "evaluation",
]


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
    degraded: bool = False
    degraded_sources: list[str] = Field(default_factory=list)
    resolved_model: str
    static_tokens: int = 0
    dynamic_tokens: int = 0
    prompt_tokens: int = 0
    max_context_tokens: int = 0
    recommend_compaction: bool = False
    cache_breakpoint_index: int | None = None


def _attachment_note(attachments: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for attachment in attachments:
        filename = str(attachment.get("filename") or attachment.get("artifact_id") or "attachment")
        kind = str(attachment.get("kind") or "file")
        parts.append(f"{filename} ({kind})")
    return "Attachments: " + ", ".join(parts)


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
        if kind == ArtifactKind.PDF.value and getattr(model_info, "supports_pdf_input", False):
            blocks.append({"type": "file", "file": {"file_url": url, "filename": filename}})
            continue
        if kind == ArtifactKind.AUDIO.value and getattr(model_info, "supports_audio_input", False):
            blocks.append({"type": "file", "file": {"file_url": url, "filename": filename}})
            continue
        if kind == ArtifactKind.FILE.value and getattr(model_info, "supports_file_input", False):
            blocks.append({"type": "file", "file": {"file_url": url, "filename": filename}})
            continue
        unsupported.append(filename)
    return blocks, unsupported


def _build_environment_info(
    executor_environment: ExecutorEnvironmentSnapshot | None = None,
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
            f"- Date: {datetime.date.today().isoformat()}\n"
            "If you omit a filesystem path or shell workdir, the executor still defaults "
            "to its own local home directory. Do not guess controller paths."
        )

    return (
        "Environment:\n"
        + (_format_executor_label(env))
        + f"- Platform: {env.platform_os or 'unknown'} ({env.platform_arch or 'unknown'})\n"
        + f"- Hostname: {env.hostname or 'unknown'}\n"
        + f"- System user: {env.user or 'unknown'}\n"
        + f"- Home directory: {env.home or 'unknown'}\n"
        + f"- Working directory: {env.cwd or 'unknown'}\n"
        f"- Date: {datetime.date.today().isoformat()}\n"
        "When the user references ~ or $HOME, use the home directory above. "
        "If a filesystem path or shell workdir is omitted, the executor defaults to its "
        "home directory; the working directory above is informational only."
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
        max_context_tokens: int,
        compaction_threshold: float,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
    ) -> None:
        self.memory = memory
        self.guardrails = guardrails
        self.llm = llm
        self.session_cache = session_cache
        self.session_manager = session_manager
        self.max_context_tokens = max_context_tokens
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
            max_context_tokens = await get_setting_value(
                db_session, "session.max_context_tokens", 128000
            )
            compaction_threshold = await get_setting_value(
                db_session, "session.compaction_threshold", 0.85
            )
        return cls(
            memory=memory,
            guardrails=guardrails,
            llm=llm,
            session_cache=session_cache,
            session_manager=session_manager,
            max_context_tokens=int(max_context_tokens)
            if isinstance(max_context_tokens, int)
            else 128000,
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
        user_message_role: str = "user",
        tool_definitions: list[ToolDefinition] | None = None,
        active_delegations: list[dict[str, Any]] | None = None,
        prior_context: list[dict[str, Any]] | None = None,
        skip_user_message: bool = False,
        skip_memory: bool = False,
        prompt_context: PromptContext = PromptContext.CHAT,
        executor_environment: ExecutorEnvironmentSnapshot | None = None,
    ) -> ContextAssemblyResult:
        """Build the LLM message list for a single turn.

        ``prior_context`` is an optional list of messages to inject after
        session history and before the user message.  Used by the workflow
        engine to inject prior step output.  Chat turns pass ``None``.

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
                user_message_role=user_message_role,
                tool_definitions=tool_definitions,
                active_delegations=active_delegations,
                prior_context=prior_context,
                skip_user_message=skip_user_message,
                prompt_context=prompt_context,
                executor_environment=executor_environment,
            )

        cached_intention = self.session_cache.get_intention(session.session_id)
        is_first_recall = session.mnemory_session_id is None

        # Check if cached instructions are stale (TTL 30 min)
        _cached_instr, _cached_core, memory_cache_valid = self.session_cache.get_cached_memory(
            session.session_id
        )
        need_instructions = is_first_recall or not memory_cache_valid
        search_mode = "find" if is_first_recall else "search"

        with scoped_runtime_context(user_email=session.user_email, agent_id=session.agent_id):
            recall_task = self.memory.recall(
                query=user_message,
                session_id=session.mnemory_session_id,
                labels=conversation.context.memory_labels,
                context=cached_intention,
                search_mode=search_mode,
                include_instructions=need_instructions,
                managed=True,
                instruction_mode="personality" if need_instructions else None,
            )
            refresh_task = self.session_cache.refresh(session)
            intention_task = self.guardrails.get_session(
                session.intaris_session_id or session.session_id
            )
            # Providers are mandatory — any failure propagates immediately.
            # Intention is best-effort (non-critical for turn execution).
            gathered_results: tuple[Any, Any, Any] = await asyncio.gather(
                recall_task,
                refresh_task,
                intention_task,
                return_exceptions=True,  # Intention may fail independently
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

            # Sync Intaris-generated title to conversation. Only writes
            # to DB when the title has actually changed to avoid churn.
            if (
                intention_result.title
                and not conversation.title
                and self.session_factory is not None
            ):
                try:
                    async with self.session_factory() as db_session:
                        ok = await update_conversation(
                            db_session,
                            conversation.conversation_id,
                            title=intention_result.title,
                        )
                        if ok:
                            await db_session.commit()
                            conversation.title = intention_result.title
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
        immutable_instructions: str | None = None
        immutable_core_memories: str | None = None
        mutable_search_results: str | None = None

        # recall_result is guaranteed to be a dict here (we raise on Exception above).
        recall_payload: dict[str, Any] = recall_result
        recall_session_id = str(recall_payload.get("session_id") or "").strip()
        if session.mnemory_session_id is None and recall_session_id:
            updated = await self.session_manager.attach_mnemory_session(
                session.session_id, recall_session_id
            )
            if updated:
                session.mnemory_session_id = recall_session_id

        # Extract the three memory parts from the recall response
        raw_instructions = recall_payload.get("instructions")
        raw_core = recall_payload.get("core_memories")
        raw_search = recall_payload.get("search_results")

        if isinstance(raw_instructions, str) and raw_instructions.strip():
            immutable_instructions = _adapt_memory_instructions(raw_instructions.strip())
        if isinstance(raw_core, str) and raw_core.strip():
            immutable_core_memories = raw_core.strip()

        # Cache immutable parts on first recall (or refresh on stale)
        if immutable_instructions or immutable_core_memories:
            await self.session_cache.cache_memory(
                session.session_id, immutable_instructions, immutable_core_memories
            )
        else:
            # Use cached values if recall didn't return new ones
            # (subsequent calls don't include instructions/core)
            cached_instr, cached_core, cache_valid = self.session_cache.get_cached_memory(
                session.session_id
            )
            if cache_valid:
                immutable_instructions = cached_instr
                immutable_core_memories = cached_core

        # Format mutable search results
        mutable_search_results = _format_search_results(raw_search)

        # Model resolution chain: session override → agent config → system default
        model_override = self.session_cache.get_model_override(session.session_id)
        explicit_model = model_override or (agent.llm_config.model if agent.llm_config else None)
        resolved_model = await self.llm.resolve_model(
            explicit_model=explicit_model,
            task_type="default",
        )
        model_info = await self.llm.get_model_info(resolved_model)
        if model_info.model_id == "unknown":
            degraded_sources.append("model_info")

        max_context_tokens = min(self.max_context_tokens, model_info.context_window)
        reserve_output_tokens = (
            agent.llm_config.max_tokens
            if agent.llm_config and agent.llm_config.max_tokens is not None
            else model_info.max_output_tokens
        )
        identity_prompt = _compose_identity_prompt(agent)
        system_prompt_tokens, tool_schema_tokens = self._count_static_tokens(
            resolved_model=resolved_model,
            system_prompt=identity_prompt,
            tool_definitions=tool_definitions or [],
        )
        static_tokens = system_prompt_tokens + tool_schema_tokens
        dynamic_tokens = max(0, max_context_tokens - static_tokens - reserve_output_tokens)
        max_prompt_tokens = max(0, max_context_tokens - reserve_output_tokens)

        # ----- Build messages: immutable prefix first, then mutable suffix -----
        messages: list[dict[str, Any]] = []

        # Immutable prefix block 1: agent identity (personality fields + system prompt).
        # Personality fields (purpose, tone, temperament, behavioral_rules) form the
        # static core identity that is always present.  The system_prompt provides
        # additional user-written instructions.  Note: personality text may also
        # appear in Mnemory core memories (block 4) after bootstrap — this is
        # intentional.  The system prompt is the guaranteed baseline; Mnemory is the
        # evolution layer that can refine or extend the agent's self-understanding.
        if identity_prompt:
            messages.append({"role": "system", "content": identity_prompt})

        # Immutable prefix block 2: system instructions (context-dependent, not editable)
        system_instructions = build_system_instructions(prompt_context, agent_id=agent.agent_id)
        if system_instructions:
            messages.append({"role": "system", "content": system_instructions})

        # Immutable prefix block 3: memory instructions (behavioral guidance)
        if immutable_instructions:
            messages.append(
                {
                    "role": "system",
                    "content": f"<memory_instructions>\n{immutable_instructions}\n</memory_instructions>",
                }
            )

        # Immutable prefix block 4: core memories (pinned facts, identity)
        if immutable_core_memories:
            messages.append(
                {
                    "role": "system",
                    "content": (
                        '<memory_context trust="untrusted">\n'
                        + immutable_core_memories
                        + "\n</memory_context>"
                    ),
                }
            )

        # Immutable prefix block 5: available skills metadata (stable, token-light)
        # Only compact metadata is included here for prompt caching stability.
        # Full instructions are loaded on demand via the skill_load tool.
        # Version ids and content hashes are intentionally excluded to keep
        # the immutable prefix stable across skill edits.
        skill_metadata = self._get_available_skills_metadata(agent)
        if skill_metadata:
            messages.append(
                {
                    "role": "system",
                    "content": (
                        skill_metadata
                        + "\n\nWhen a skill is relevant to the current task, use the "
                        "skill_load tool to read its full instructions before proceeding. "
                        "Do not guess skill behavior from the summary alone."
                    ),
                }
            )

        # Immutable prefix block 6: compaction summary (stable within session)
        compaction_summary = cache_entry.last_compaction_summary
        if compaction_summary:
            messages.append(
                {
                    "role": "system",
                    "content": (
                        "This is a continuation from a previous session. "
                        "Here is a summary of what was discussed:\n\n"
                        f"{compaction_summary}\n\n"
                        "The conversation continues below."
                    ),
                }
            )

        # ----- Mutable suffix -----

        messages.append(
            {
                "role": "system",
                "content": _build_environment_info(executor_environment),
            }
        )

        # History messages (append-only)
        history_messages = self._events_to_messages(
            self.session_cache.get_events_since_compaction(
                session.session_id, EVENT_TYPES_FOR_CONTEXT
            )
        )
        messages.extend(history_messages)

        # Per-turn recalled memories (mutable, appended each turn)
        if mutable_search_results:
            messages.append(
                {
                    "role": "system",
                    "content": (
                        '<memory_context trust="untrusted">\n'
                        "Recalled memories:\n" + mutable_search_results + "\n</memory_context>"
                    ),
                }
            )

        if active_delegations:
            messages.append(
                {"role": "system", "content": _format_active_delegations(active_delegations)}
            )

        # Prior context from caller (e.g. prior workflow step output).
        # Mark these messages so _prune_messages can protect them.
        if prior_context:
            for msg in prior_context:
                msg["_prior_context"] = True
            messages.extend(prior_context)
            logger.debug(
                "context: injecting prior step context",
                extra={"extra_data": {"message_count": len(prior_context)}},
            )

        if not skip_user_message:
            if attachment_notice:
                messages.append({"role": "system", "content": attachment_notice})
            attachment_blocks, unsupported = _native_attachment_blocks(
                user_attachments or [], model_info
            )
            if attachment_blocks:
                blocks: list[dict[str, Any]] = []
                if user_message.strip():
                    blocks.append({"type": "text", "text": user_message})
                elif attachment_blocks:
                    blocks.append({"type": "text", "text": "User attached files."})
                blocks.extend(attachment_blocks)
                if unsupported:
                    blocks.append(
                        {
                            "type": "text",
                            "text": _attachment_note(
                                _filter_attachments_by_names(user_attachments or [], unsupported)
                            ),
                        }
                    )
                messages.append({"role": user_message_role, "content": blocks})
            else:
                content = user_message
                if user_attachments:
                    note = _attachment_note(user_attachments)
                    content = f"{user_message}\n\n{note}" if user_message.strip() else note
                messages.append({"role": user_message_role, "content": content})

        messages = self._prune_messages(
            messages=messages,
            resolved_model=resolved_model,
            max_prompt_tokens=max_prompt_tokens,
            system_prompt=identity_prompt,
            tool_schema_tokens=tool_schema_tokens,
        )

        # Strip internal markers before sending to LLM
        for msg in messages:
            msg.pop("_prior_context", None)

        # Recompute cache breakpoint after pruning (immutable messages may have shifted)
        cache_breakpoint_index = _find_cache_breakpoint(messages)

        prompt_tokens = (
            self.llm.count_messages_tokens(messages, resolved_model) + tool_schema_tokens
        )
        recommend_compaction = (
            max_context_tokens > 0
            and (prompt_tokens / max_context_tokens) >= self.compaction_threshold
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
        user_message_role: str = "user",
        tool_definitions: list[ToolDefinition] | None = None,
        active_delegations: list[dict[str, Any]] | None = None,
        prior_context: list[dict[str, Any]] | None = None,
        skip_user_message: bool = False,
        prompt_context: PromptContext = PromptContext.TASK_STEP,
        executor_environment: ExecutorEnvironmentSnapshot | None = None,
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
        resolved_model = await self.llm.resolve_model(
            explicit_model=explicit_model,
            task_type="default",
        )
        model_info = await self.llm.get_model_info(resolved_model)
        if model_info.model_id == "unknown":
            degraded_sources.append("model_info")

        max_context_tokens = min(self.max_context_tokens, model_info.context_window)
        reserve_output_tokens = (
            agent.llm_config.max_tokens
            if agent.llm_config and agent.llm_config.max_tokens is not None
            else model_info.max_output_tokens
        )
        identity_prompt = _compose_identity_prompt(agent)
        system_prompt_tokens, tool_schema_tokens = self._count_static_tokens(
            resolved_model=resolved_model,
            system_prompt=identity_prompt,
            tool_definitions=tool_definitions or [],
        )
        static_tokens = system_prompt_tokens + tool_schema_tokens
        dynamic_tokens = max(0, max_context_tokens - static_tokens - reserve_output_tokens)
        max_prompt_tokens = max(0, max_context_tokens - reserve_output_tokens)

        # Build messages: identity + system instructions + env + compaction + history
        messages: list[dict[str, Any]] = []

        if identity_prompt:
            messages.append({"role": "system", "content": identity_prompt})

        # System instructions (context-dependent, not editable)
        system_instructions = build_system_instructions(prompt_context, agent_id=agent.agent_id)
        if system_instructions:
            messages.append({"role": "system", "content": system_instructions})

        # No memory instructions, no core memories (skip_memory)

        compaction_summary = cache_entry.last_compaction_summary
        if compaction_summary:
            messages.append(
                {
                    "role": "system",
                    "content": (
                        "This is a continuation from a previous session. "
                        "Here is a summary of what was discussed:\n\n"
                        f"{compaction_summary}\n\n"
                        "The conversation continues below."
                    ),
                }
            )

        messages.append(
            {
                "role": "system",
                "content": _build_environment_info(executor_environment),
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
            messages.extend(prior_context)

        if not skip_user_message:
            if attachment_notice:
                messages.append({"role": "system", "content": attachment_notice})
            attachment_blocks, unsupported = _native_attachment_blocks(
                user_attachments or [], model_info
            )
            if attachment_blocks:
                blocks: list[dict[str, Any]] = []
                if user_message.strip():
                    blocks.append({"type": "text", "text": user_message})
                else:
                    blocks.append({"type": "text", "text": "User attached files."})
                blocks.extend(attachment_blocks)
                if unsupported:
                    blocks.append(
                        {
                            "type": "text",
                            "text": _attachment_note(
                                _filter_attachments_by_names(user_attachments or [], unsupported)
                            ),
                        }
                    )
                messages.append({"role": user_message_role, "content": blocks})
            else:
                content = user_message
                if user_attachments:
                    note = _attachment_note(user_attachments)
                    content = f"{user_message}\n\n{note}" if user_message.strip() else note
                messages.append({"role": user_message_role, "content": content})

        messages = self._prune_messages(
            messages=messages,
            resolved_model=resolved_model,
            max_prompt_tokens=max_prompt_tokens,
            system_prompt=identity_prompt,
            tool_schema_tokens=tool_schema_tokens,
        )

        for msg in messages:
            msg.pop("_prior_context", None)

        cache_breakpoint_index = _find_cache_breakpoint(messages)
        prompt_tokens = (
            self.llm.count_messages_tokens(messages, resolved_model) + tool_schema_tokens
        )
        recommend_compaction = (
            max_context_tokens > 0
            and (prompt_tokens / max_context_tokens) >= self.compaction_threshold
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

    def _count_static_tokens(
        self,
        *,
        resolved_model: str,
        system_prompt: str | None,
        tool_definitions: list[ToolDefinition],
    ) -> tuple[int, int]:
        system_prompt_tokens = 0
        tool_schema_tokens = 0
        if system_prompt:
            system_prompt_tokens += self.llm.count_tokens(system_prompt, resolved_model)
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
        system_prompt: str | None,
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
            indices_to_drop = _find_oldest_droppable_group(pruned_messages, system_prompt)
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
                    content = f"{content}\n\n{_attachment_note([a for a in attachments if isinstance(a, dict)])}"
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


def _find_cache_breakpoint(messages: list[dict[str, Any]]) -> int | None:
    """Find the index of the last immutable prefix message.

    The cache breakpoint is the last system message that belongs to the
    immutable prefix (system prompt, memory instructions, core memories,
    or compaction summary). Everything after this index is mutable and
    changes every turn.

    The system prompt (first message) is always immutable. We detect
    the other immutable messages by their content markers.
    """

    if not messages:
        return None

    # The first message is always the system prompt (immutable)
    system_prompt = messages[0].get("content") if messages[0].get("role") == "system" else None

    last_immutable = None
    for index, message in enumerate(messages):
        if _is_immutable_prefix_message(message, system_prompt):
            last_immutable = index
        elif message.get("role") != "system":
            # First non-system message means we've left the prefix
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
    system_prompt: str | None,
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
        if _is_immutable_prefix_message(msg, system_prompt):
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


def _is_immutable_prefix_message(message: dict[str, Any], system_prompt: str | None) -> bool:
    """Check if a message belongs to the immutable prompt prefix.

    Protected messages that should never be pruned:
    - System prompt
    - Memory instructions (server-generated behavioral guidance)
    - Core memories (untrusted wrapper with pinned facts)
    - Compaction summary (continuation context)
    - Prior step context (workflow step output from a previous step)
    """
    # Prior step context is critical for workflow step continuity
    if message.get("_prior_context"):
        return True
    if message.get("role") != "system":
        return False
    content = message.get("content")
    if not isinstance(content, str):
        return False
    # System prompt
    if system_prompt is not None and content == system_prompt:
        return True
    # Memory instructions (server-generated behavioral guidance)
    if content.startswith("<memory_instructions>"):
        return True
    # Compaction summary
    if content.startswith("This is a continuation from a previous session."):
        return True
    # Core memories (untrusted wrapper without "Recalled memories" marker)
    return '<memory_context trust="untrusted">' in content and "Recalled memories:" not in content


# ---------------------------------------------------------------------------
# Instruction adaptation: MCP tool names → Cognis builtin tool names
# ---------------------------------------------------------------------------

# Mnemory instructions reference MCP tool names (search_memories, add_memory,
# etc.) but Cognis exposes builtin tools with different names (memory_search,
# memory_add, etc.).  This mapping translates instruction text so the LLM
# can connect the guidance to the actual tools it has available.
#
# Ordered longest-first within each group to prevent partial-match collisions
# (e.g. "add_memories" must be replaced before "add_memory").
_MCP_TO_COGNIS_TOOL_NAMES: list[tuple[str, str]] = [
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
]

# MCP tools that don't exist in Cognis — handled automatically by the
# controller.  References in instructions are annotated so the LLM knows
# these are not callable.  List (not set) for deterministic iteration.
_MCP_NONEXISTENT_TOOLS: list[str] = ["initialize_memory", "get_core_memories"]

# Pre-compiled regex patterns for each mapping (word-boundary safe).
_TOOL_NAME_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(rf"\b{re.escape(mcp_name)}\b"), cognis_name)
    for mcp_name, cognis_name in _MCP_TO_COGNIS_TOOL_NAMES
]

# Pre-compiled patterns for non-existent tools.
_NONEXISTENT_TOOL_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(rf"\b{re.escape(name)}\b"), f"{name} (not available)")
    for name in _MCP_NONEXISTENT_TOOLS
]


def _adapt_memory_instructions(instructions: str) -> str:
    """Adapt mnemory instructions for Cognis builtin tool names.

    Replaces MCP-style tool names (search_memories, add_memory, etc.) with
    Cognis builtin names (memory_search, memory_add, etc.) so the LLM can
    connect the behavioral guidance to the actual tools in its schema.

    Also annotates references to tools that don't exist in Cognis
    (initialize_memory, get_core_memories) with "(not available)".
    """
    text = instructions

    # Annotate references to non-existent tools first (before renaming).
    for pattern, replacement in _NONEXISTENT_TOOL_PATTERNS:
        text = pattern.sub(replacement, text)

    # Replace MCP tool names with Cognis builtin names.
    for pattern, replacement in _TOOL_NAME_PATTERNS:
        text = pattern.sub(replacement, text)

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
