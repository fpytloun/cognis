"""Composable system instructions injected at runtime.

These instructions are NOT part of the user-editable agent system prompt.
They are assembled by the controller based on execution context and injected
as a separate system message after the agent's identity prompt.

The agent's ``system_prompt`` field (stored in DB, editable in UI) contains
only the agent's identity: name, description, personality, behavioral rules.
Everything below is injected transparently — the user never sees or edits it.
"""

from __future__ import annotations

from enum import Enum


class PromptContext(Enum):
    """Execution context that determines which system instructions to inject."""

    CHAT = "chat"
    """Interactive chat turn (WebSocket). Full routing guidance."""

    TASK_STEP = "task_step"
    """Executing inside a workflow step. Step-focused instructions."""

    DELEGATION = "delegation"
    """Running a sub-session (delegate/worker/fork). Focused on returning results."""


# ---------------------------------------------------------------------------
# Instruction sections
# ---------------------------------------------------------------------------

_CORE_BEHAVIOR = """\
## Behavior

- Be direct and concise. Avoid filler, unnecessary praise, or emotional \
validation.
- Prioritize technical accuracy over agreement. Disagree when warranted.
- When uncertain, investigate before answering — do not guess or fabricate.
- Use the user's language for conversation but keep all code, comments, \
and identifiers in English.
- When referencing code, include file paths and line numbers \
(e.g. `src/main.py:42`)."""

_TOOL_GUIDANCE = """\
## Tool usage

- Prefer specialized tools over shell commands: use file read/write/edit \
tools instead of cat/sed/awk, use search/glob tools instead of grep/find \
via shell.
- Reserve shell execution for actual system commands that need a terminal.
- When a tool call fails, analyze the error before retrying. Do not retry \
blindly.
- Make independent tool calls in parallel when possible for efficiency.
- Large outputs are automatically truncated. Use offset/limit parameters \
or search tools to navigate large files."""

_CONTEXT_AWARENESS = """\
## Context

- Memory context (facts, preferences, personality) is injected \
automatically into this conversation. Use recalled memories naturally — \
do not ask the user for information that may already be in your context.
- Environment information (platform, working directory, date) is provided \
in a separate system message.
- If the conversation has been compacted, a summary of prior context is \
included. Continue naturally from where it left off."""

_WORK_ROUTING = """\
## Work routing

Decide how to handle each request based on complexity:

- **Inline** (handle it yourself): Simple questions, quick lookups, \
single-file edits, short tasks requiring 1-3 tool calls. Fast but blocks \
the chat until done.
- **Delegate** (sub-session): Focused tasks needing multiple tool calls — \
exploration, research, targeted implementation. The main chat stays \
responsive while the sub-session works. Use for anything taking 5+ tool \
calls with a clear scope.
- **Task** (background workflow): Complex multi-step work that benefits \
from planning, evaluation, and review gates. Use for feature \
implementation, refactoring, research projects — anything where quality \
checkpoints and structured steps matter.

When in doubt between inline and delegate, prefer delegation for anything \
non-trivial. The user can continue chatting while delegated work runs in \
the background.

Do not create tasks for simple requests. A task implies a structured \
workflow with planning, execution, and review — use it when that structure \
adds value."""

_STEP_EXECUTION = """\
## Step execution

You are executing a workflow step. Focus entirely on the step objective.

- Work through the objective methodically. Use step todos to track \
progress on multi-part work.
- When finished, write out your findings and deliverables as a detailed \
text response. Then call `step_complete` with a summary, structured \
outputs, and verifiable claims.
- The evaluator checks your work against your written output and claims — \
be thorough and specific. Vague summaries get rejected.
- If you need clarification, use `step_request_input` (when available) \
rather than guessing.
- Stay within the step's scope. Do not create new tasks or make decisions \
outside the step objective."""

_DELEGATION_FOCUS = """\
## Sub-session

You are running a focused sub-session delegated from a parent conversation. \
Complete the specific task you were given and return a clear, actionable \
result.

- Stay focused on the delegated task. Do not branch into unrelated work.
- Write a comprehensive result when done, then call `step_complete` with \
a summary.
- Delegate further only if the task genuinely requires it — prefer doing \
the work directly."""


# ---------------------------------------------------------------------------
# Hidden agent prompt overrides (evaluator, classifier, compaction)
# ---------------------------------------------------------------------------

# These agents have very specific output format requirements and should NOT
# receive the generic system instructions. They are identified by agent_id.
_SKIP_SYSTEM_INSTRUCTIONS: frozenset[str] = frozenset(
    {
        "system:evaluator",
        "system:classifier",
        "system:compaction",
    }
)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def build_system_instructions(
    context: PromptContext,
    *,
    agent_id: str | None = None,
) -> str | None:
    """Assemble context-appropriate system instructions.

    Returns ``None`` for hidden system agents that should not receive
    generic instructions (evaluator, classifier, compaction).

    Parameters
    ----------
    context:
        The execution context (chat, task_step, delegation).
    agent_id:
        The agent's ID. Hidden system agents are skipped.
    """
    if agent_id and agent_id in _SKIP_SYSTEM_INSTRUCTIONS:
        return None

    sections: list[str] = [_CORE_BEHAVIOR, _TOOL_GUIDANCE, _CONTEXT_AWARENESS]

    if context == PromptContext.CHAT:
        sections.append(_WORK_ROUTING)
    elif context == PromptContext.TASK_STEP:
        sections.append(_STEP_EXECUTION)
    elif context == PromptContext.DELEGATION:
        sections.append(_DELEGATION_FOCUS)

    return "\n\n".join(sections)
