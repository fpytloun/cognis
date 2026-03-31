"""Step context assembly for workflow steps.

Assembles the initial LLM context for a workflow step according to
its ``StepInputConfig`` (null / full / summary / last).  Composes with
the base ``ContextAssembler`` for memory, system prompt, and session
history, then injects step-specific input context blocks and the step
prompt.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from prometheus_client import Counter, Histogram

from cognis.core.context import ContextAssembler, ContextAssemblyResult, events_to_messages
from cognis.logging import get_logger
from cognis.models.agent import AgentDefinition
from cognis.models.session import ConversationModel, SessionModel
from cognis.models.workflow import (
    StepDefinition,
    StepInputConfig,
    StepOutput,
    WorkflowState,
    resolve_effective_input,
)

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Prometheus metrics
# ---------------------------------------------------------------------------

FULL_TO_SUMMARY_FALLBACK = Counter(
    "cognis_step_context_full_to_summary_fallback_total",
    "Full input type fell back to summary due to token budget overflow",
)
SUMMARY_TO_LAST_FALLBACK = Counter(
    "cognis_step_context_summary_to_last_fallback_total",
    "Summary generation failed or timed out — fell back to last",
)
CACHE_BYPASS_DIRECT_READ = Counter(
    "cognis_step_context_cache_bypass_direct_read_total",
    "Direct Intaris read bypassed session cache (evicted entry)",
)
SUMMARY_DURATION = Histogram(
    "cognis_step_context_summary_duration_seconds",
    "Duration of step-summary LLM generation",
)
FULL_LOAD_DURATION = Histogram(
    "cognis_step_context_full_load_duration_seconds",
    "Duration of full-session event loading",
)

# Default summary generation limits
_DEFAULT_SUMMARY_TIMEOUT_SECONDS = 30.0
_DEFAULT_SUMMARY_MAX_TOKENS = 500


class StepContextAssembler:
    """Assemble context for a first-attempt workflow step.

    On retries (``is_retry=True`` on StepContext), the caller should use
    the regular ``ContextAssembler`` instead — the existing session already
    contains the original step prompt, prior work, and evaluation feedback.
    """

    def __init__(
        self,
        *,
        context_assembler: ContextAssembler,
        session_cache: Any,
        guardrails: Any,
        llm: Any,
        summary_timeout_seconds: float = _DEFAULT_SUMMARY_TIMEOUT_SECONDS,
        summary_max_tokens: int = _DEFAULT_SUMMARY_MAX_TOKENS,
    ) -> None:
        self.context_assembler = context_assembler
        self.session_cache = session_cache
        self.guardrails = guardrails
        self.llm = llm
        self.summary_timeout_seconds = summary_timeout_seconds
        self.summary_max_tokens = summary_max_tokens

    async def assemble(
        self,
        *,
        session: SessionModel,
        conversation: ConversationModel,
        agent: AgentDefinition,
        step_definition: StepDefinition,
        step_index: int,
        workflow_steps: list[StepDefinition],
        workflow_state: WorkflowState,
        step_prompt: str,
    ) -> ContextAssemblyResult:
        """Build the full LLM context for a first-attempt workflow step.

        1. Base context (system prompt, compaction, memory, session history)
           via ``ContextAssembler.assemble(skip_user_message=True)``.
        2. Step input context blocks (null / full / summary / last).
        3. Final step prompt as user message.
        """
        # --- 1. Base context (no user message yet) ---
        base_result = await self.context_assembler.assemble(
            session=session,
            conversation=conversation,
            agent=agent,
            user_message=step_prompt,
            skip_user_message=True,
        )
        messages = base_result.messages

        # --- 2. Step input context ---
        effective_input = resolve_effective_input(step_definition, step_index, workflow_steps)
        input_messages = await self._assemble_step_input(
            effective_input=effective_input,
            workflow_state=workflow_state,
            base_result=base_result,
            resolved_model=base_result.resolved_model,
        )
        messages.extend(input_messages)

        # --- 3. Step prompt ---
        messages.append({"role": "user", "content": step_prompt})

        # Recount tokens to include step input messages and step prompt
        # (base_result.prompt_tokens only covers the base context).
        total_prompt_tokens = self.llm.count_messages_tokens(messages, base_result.resolved_model)

        return ContextAssemblyResult(
            messages=messages,
            degraded=base_result.degraded,
            degraded_sources=base_result.degraded_sources,
            resolved_model=base_result.resolved_model,
            static_tokens=base_result.static_tokens,
            dynamic_tokens=base_result.dynamic_tokens,
            prompt_tokens=total_prompt_tokens,
            recommend_compaction=base_result.recommend_compaction,
        )

    # ------------------------------------------------------------------
    # Input-type dispatchers
    # ------------------------------------------------------------------

    async def _assemble_step_input(
        self,
        *,
        effective_input: StepInputConfig,
        workflow_state: WorkflowState,
        base_result: ContextAssemblyResult,
        resolved_model: str,
    ) -> list[dict[str, Any]]:
        """Dispatch to the correct assembler based on input type."""
        if effective_input.type == "null":
            return []
        if effective_input.type == "last":
            return self._assemble_last(effective_input, workflow_state)
        if effective_input.type == "full":
            return await self._assemble_full(
                effective_input, workflow_state, base_result, resolved_model
            )
        if effective_input.type == "summary":
            return await self._assemble_summary(effective_input, workflow_state)
        return []

    # ------------------------------------------------------------------
    # last
    # ------------------------------------------------------------------

    def _assemble_last(
        self,
        config: StepInputConfig,
        state: WorkflowState,
    ) -> list[dict[str, Any]]:
        """Inject step_complete outputs from source steps."""
        messages: list[dict[str, Any]] = []
        for source_name in config.source_names():
            raw = state.step_outputs.get(source_name)
            if raw is None:
                continue
            output = StepOutput.model_validate(raw)
            claims_str = "\n".join(f"  - {c}" for c in output.claims) if output.claims else "(none)"
            outputs_str = json.dumps(output.outputs, default=str)[:2000] if output.outputs else "{}"
            messages.append(
                {
                    "role": "system",
                    "content": (
                        f'<step_output source="{source_name}">\n'
                        f"Summary: {output.summary}\n"
                        f"Claims:\n{claims_str}\n"
                        f"Outputs: {outputs_str}\n"
                        f"</step_output>"
                    ),
                }
            )
        return messages

    # ------------------------------------------------------------------
    # full
    # ------------------------------------------------------------------

    async def _assemble_full(
        self,
        config: StepInputConfig,
        state: WorkflowState,
        base_result: ContextAssemblyResult,
        resolved_model: str,
    ) -> list[dict[str, Any]]:
        """Inject complete event history from a single source step session."""
        source_name = config.single_source()
        if source_name is None:
            return []

        with FULL_LOAD_DURATION.time():
            full_messages = await self._load_source_events_as_messages(source_name, state)

        # Token budget check: would adding these messages exceed prompt budget?
        if base_result.dynamic_tokens > 0:
            full_tokens = self.llm.count_messages_tokens(full_messages, resolved_model)
            base_tokens = self.llm.count_messages_tokens(base_result.messages, resolved_model)
            if base_tokens + full_tokens > base_result.dynamic_tokens:
                FULL_TO_SUMMARY_FALLBACK.inc()
                logger.info(
                    "Full input exceeds token budget, falling back to summary",
                    extra={
                        "extra_data": {
                            "source_step": source_name,
                            "full_tokens": full_tokens,
                            "budget": base_result.dynamic_tokens,
                        }
                    },
                )
                return await self._assemble_summary(
                    StepInputConfig(type="summary", source=source_name), state
                )

        return full_messages

    async def _load_source_events_as_messages(
        self,
        source_name: str,
        state: WorkflowState,
    ) -> list[dict[str, Any]]:
        """Load full event history for a source step and format as messages."""
        try:
            intaris_session_id = state.get_source_intaris_session_id(source_name)
        except (ValueError, KeyError):
            logger.warning(
                "step context: source step missing intaris_session_id, "
                "falling back to step_outputs content",
                extra={"extra_data": {"source_step": source_name}},
            )
            return self._step_output_as_messages(source_name, state)

        # Try session cache first
        raw_output = state.step_outputs.get(source_name, {})
        cognis_session_id = raw_output.get("session_id")
        if cognis_session_id:
            cache_entry = self.session_cache.get_entry(cognis_session_id)
            if cache_entry is not None and cache_entry.initialized:
                return events_to_messages(cache_entry.events)

        # Cache miss — direct Intaris read.
        # Use allow_missing_stream so a source step whose events were never
        # persisted (e.g. identity mismatch before the runtime-context fix)
        # degrades to an empty list instead of crashing with a 404.
        CACHE_BYPASS_DIRECT_READ.inc()
        try:
            event_read = await self.guardrails.read_events(
                session_id=intaris_session_id,
                after_seq=0,
                allow_missing_stream=True,
            )
            if event_read.events:
                return events_to_messages(event_read.events)
            logger.warning(
                "step context: source step has no events in Intaris, "
                "falling back to step_outputs content",
                extra={
                    "extra_data": {"source_step": source_name, "session_id": intaris_session_id}
                },
            )
        except Exception:
            logger.warning(
                "step context: failed to load source step events, "
                "falling back to step_outputs content",
                extra={
                    "extra_data": {"source_step": source_name, "session_id": intaris_session_id}
                },
                exc_info=True,
            )
        # Fall back to step_outputs summary/content when Intaris returned
        # empty events or the read failed entirely.
        return self._step_output_as_messages(source_name, state)

    # ------------------------------------------------------------------
    @staticmethod
    def _step_output_as_messages(
        source_name: str,
        state: WorkflowState,
    ) -> list[dict[str, Any]]:
        """Convert a step's stored output into context messages.

        Used as a fallback when the full event history cannot be loaded
        from Intaris (empty stream, read failure, or missing session ID).
        """
        raw = state.step_outputs.get(source_name, {})
        content = raw.get("content", "")
        summary = raw.get("summary", "")
        fallback_text = content or summary
        if fallback_text:
            return [
                {
                    "role": "system",
                    "content": f"Output from step '{source_name}':\n\n{fallback_text}",
                }
            ]
        return []

    # summary
    # ------------------------------------------------------------------

    async def _assemble_summary(
        self,
        config: StepInputConfig,
        state: WorkflowState,
    ) -> list[dict[str, Any]]:
        """Generate LLM summaries of source step sessions.

        Uses ``asyncio.gather`` for parallel generation across sources,
        then injects in declared order.
        """
        source_names = config.source_names()
        if not source_names:
            return []

        tasks = [self._generate_summary(name, state) for name in source_names]
        results: list[str | None] = await asyncio.gather(*tasks, return_exceptions=False)

        messages: list[dict[str, Any]] = []
        for source_name, summary_text in zip(source_names, results, strict=True):
            if summary_text is None:
                # Fallback to last — summary generation failed
                last_msgs = self._assemble_last(
                    StepInputConfig(type="last", source=source_name), state
                )
                messages.extend(last_msgs)
            else:
                messages.append(
                    {
                        "role": "system",
                        "content": (
                            f'<step_context source="{source_name}" type="summary">\n'
                            f"{summary_text}\n"
                            f"</step_context>"
                        ),
                    }
                )
        return messages

    async def _generate_summary(
        self,
        source_name: str,
        state: WorkflowState,
    ) -> str | None:
        """Generate an LLM summary of a source step session.

        Returns None on failure / timeout (caller should degrade to ``last``).
        """
        with SUMMARY_DURATION.time():
            try:
                state.get_source_intaris_session_id(source_name)  # validates session ref exists
            except ValueError:
                SUMMARY_TO_LAST_FALLBACK.inc()
                return None

            try:
                # Load events
                event_messages = await self._load_source_events_as_messages(source_name, state)
                if not event_messages:
                    # No events — fall back to step_complete output
                    SUMMARY_TO_LAST_FALLBACK.inc()
                    return None

                conversation_text = "\n".join(
                    f"{msg['role']}: {msg.get('content', '')}" for msg in event_messages
                )
                prompt = (
                    "Summarize the following workflow step session concisely. "
                    "Focus on key decisions, outputs, and conclusions.\n\n"
                    f"{conversation_text}"
                )

                response = await asyncio.wait_for(
                    self.llm.generate(
                        [
                            {
                                "role": "system",
                                "content": "You are a workflow step summarizer. Be concise and factual.",
                            },
                            {"role": "user", "content": prompt},
                        ],
                        task_type="step_summary",
                        temperature=0,
                        max_tokens=self.summary_max_tokens,
                    ),
                    timeout=self.summary_timeout_seconds,
                )

                # Extract text from response
                choices = response.get("choices", [])
                if choices:
                    message = choices[0].get("message", {})
                    content = message.get("content")
                    if isinstance(content, str) and content.strip():
                        return content.strip()

                SUMMARY_TO_LAST_FALLBACK.inc()
                return None

            except TimeoutError:
                logger.warning(
                    "Step summary generation timed out",
                    extra={"extra_data": {"source_step": source_name}},
                )
                SUMMARY_TO_LAST_FALLBACK.inc()
                return None
            except Exception:
                logger.warning(
                    "Step summary generation failed",
                    extra={"extra_data": {"source_step": source_name}},
                )
                SUMMARY_TO_LAST_FALLBACK.inc()
                return None
