"""Context assembly for chat turns."""

from __future__ import annotations

import asyncio
import json
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from cognis.logging import get_logger
from cognis.models.agent import AgentDefinition
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
        user_message_role: str = "user",
        tool_definitions: list[ToolDefinition] | None = None,
        active_delegations: list[dict[str, Any]] | None = None,
        skip_user_message: bool = False,
    ) -> ContextAssemblyResult:
        """Build the LLM message list for a single turn."""

        logger.debug(
            "context: assembly started",
            extra={"extra_data": {"session_id": session.session_id, "agent_id": agent.agent_id}},
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
            gathered_results: tuple[Any, Any, Any] = await asyncio.gather(
                recall_task,
                refresh_task,
                intention_task,
                return_exceptions=True,
            )
        recall_result, cache_result, intention_result = gathered_results

        degraded_sources: list[str] = []

        if isinstance(cache_result, Exception):
            cache_entry = self.session_cache.get_entry(session.session_id)
            if cache_entry is None or not cache_entry.initialized:
                raise cache_result
            logger.warning(
                "context: degraded source=events",
                extra={"extra_data": {"session_id": session.session_id}},
            )
            degraded_sources.append("events")
        else:
            cache_entry = cache_result

        if isinstance(intention_result, Exception):
            logger.warning(
                "context: degraded source=intention",
                extra={"extra_data": {"session_id": session.session_id}},
            )
            degraded_sources.append("intention")
        else:
            await self.session_cache.update_intention(
                session.session_id, intention_result.intention
            )
            cache_entry.intention = intention_result.intention

            # Sync Intaris-generated title to conversation. Only writes
            # to DB when the title has actually changed to avoid churn.
            if (
                intention_result.title
                and conversation.title != intention_result.title
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

        if isinstance(recall_result, Exception):
            logger.warning(
                "context: degraded source=memory",
                extra={"extra_data": {"session_id": session.session_id}},
            )
            degraded_sources.append("memory")

            # Fall back to cached immutable memory if available
            cached_instr, cached_core, cache_valid = self.session_cache.get_cached_memory(
                session.session_id
            )
            if cached_instr or cached_core:
                immutable_instructions = cached_instr
                immutable_core_memories = cached_core
        else:
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
                immutable_instructions = raw_instructions.strip()
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

        resolved_model = await self.llm.resolve_model(
            explicit_model=agent.llm_config.model if agent.llm_config else None,
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
        system_prompt_tokens, tool_schema_tokens = self._count_static_tokens(
            resolved_model=resolved_model,
            system_prompt=agent.system_prompt,
            tool_definitions=tool_definitions or [],
        )
        static_tokens = system_prompt_tokens + tool_schema_tokens
        dynamic_tokens = max(0, max_context_tokens - static_tokens - reserve_output_tokens)
        max_prompt_tokens = max(0, max_context_tokens - reserve_output_tokens)

        # ----- Build messages: immutable prefix first, then mutable suffix -----
        messages: list[dict[str, Any]] = []

        # Immutable prefix block 1: system prompt
        if agent.system_prompt:
            messages.append({"role": "system", "content": agent.system_prompt})

        # Immutable prefix block 2: memory instructions (behavioral guidance)
        if immutable_instructions:
            messages.append(
                {
                    "role": "system",
                    "content": f"<memory_instructions>\n{immutable_instructions}\n</memory_instructions>",
                }
            )

        # Immutable prefix block 3: core memories (pinned facts, identity)
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

        # Immutable prefix block 4: compaction summary (stable within session)
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
        if not skip_user_message:
            messages.append({"role": user_message_role, "content": user_message})

        messages = self._prune_messages(
            messages=messages,
            resolved_model=resolved_model,
            max_prompt_tokens=max_prompt_tokens,
            system_prompt=agent.system_prompt,
            tool_schema_tokens=tool_schema_tokens,
        )

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

            # Priority 2: Drop oldest non-protected messages (history)
            removable_history_index = next(
                (
                    index
                    for index, message in enumerate(pruned_messages)
                    if index != len(pruned_messages) - 1
                    and not _is_immutable_prefix_message(message, system_prompt)
                ),
                None,
            )
            if removable_history_index is None:
                break
            pruned_messages.pop(removable_history_index)
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

    def _flush_tool_calls() -> None:
        """Flush buffered tool_call events into an assistant message."""
        if not pending_tool_calls:
            return
        tc_array = list(pending_tool_calls)
        pending_tool_calls.clear()
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
            content = event_data.get("content")
            if isinstance(content, str):
                messages.append({"role": "user", "content": content})
        elif event_type == "assistant_message":
            content = event_data.get("content")
            if isinstance(content, str):
                messages.append({"role": "assistant", "content": content})
        elif event_type == "tool_result":
            # The agent loop stores tool output under key "result";
            # fall back to "output" for forward-compatibility.
            output = event_data.get("result") or event_data.get("output")
            call_id = event_data.get("call_id", "")
            if isinstance(output, str):
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call_id,
                        "content": output,
                    }
                )
        elif event_type == "delegation":
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
            # Other lifecycle events (task_status, etc.) are informational — skip
        elif event_type == "evaluation":
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
    # Flush any remaining buffered tool calls at the end of the event list
    _flush_tool_calls()
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


def _is_immutable_prefix_message(message: dict[str, Any], system_prompt: str | None) -> bool:
    """Check if a message belongs to the immutable prompt prefix.

    Protected messages that should never be pruned:
    - System prompt
    - Memory instructions (server-generated behavioral guidance)
    - Core memories (untrusted wrapper with pinned facts)
    - Compaction summary (continuation context)
    """
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
