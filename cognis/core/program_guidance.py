"""Canonical controller-side programmatic-tool-calling guidance.

The wording below adapts patterns from Prime Intellect's ``prime-agent`` P1
``core.md``, ``ipython.rs``, and ``usage.md`` (MIT, revision
``8aac8692456c00c01d4e53e60e6d3347a3506559``); ``nano-rlm`` N1–N3
``prompt.py``, ``tools/ipython.py``, and ``compaction.py`` (MIT, revision
``84110bcb73e3c1acaf69047fda50aa4dff15eea1``); and ``smolagents`` S1
``code_agent.yaml`` (Apache-2.0, revision
``12c1bc820eca50ace6f80a21d90426d41d74f845``), plus the OpenAI PTC guide at
``https://developers.openai.com/api/docs/guides/tools-programmatic-tool-calling``
(retrieved 2026-10-22). These are adaptations, not quotations. The portable
runtime guide is owned by ``cognis.program_guidance`` in ``cognis-common``.
"""

from __future__ import annotations

from cognis.program_guidance import render_runtime_guide as render_runtime_guide

PYTHON_EXECUTE_TOOL_NAME = "python_execute"

PYTHON_EXECUTE_DESCRIPTION = """\
Execute a Python cell in this conversation's executor kernel, with top-level await.
Provide a required description explaining what you will do and why, as for bash.
Variables and helpers persist while the kernel lives; restart or reclamation loses
them, so read state-loss notices before continuing. Use the generated Cognis API
described in the runtime guide. Every invocation remains subject to normal tool
policy and approval.
Finish emitted results with `await cognis.emit(json_value)`.
Python/IPython is not a sandbox and does not replace dedicated tools."""

PROGRAM_ROUTING_GUIDANCE = """\
## Programmatic tool execution

- Use `python_execute` for a bounded read stage with predictable loops or
  dependent calls, especially fan-out, filtering, joining, or aggregation that
  returns much less data. Prefer direct calls for one lookup, adaptive judgment,
  writes, approval-sensitive actions, and final citation or artifact validation.
- Describe what this execution will do and why. Define eligible tools, required
  result fields/evidence, a stopping condition, and a finite retry allowance.
  Parallelize only independent safe reads. Return a small structured result;
  do not redo completed calls. Missing required data must be explicit, not guessed.
- Do not start nested agents.
- Finish with `await cognis.emit(json_value)`; printing is diagnostic, not completion.
- Keep credentials out of `inputs`; Python code can inspect every value it receives.
- Inspect errors before retrying. An approval request suspends its call; await
  the ordinary resolution rather than replacing it with another call. Respect a
  denial without rerouting to bypass it. Cancellation and uncertain effects are
  not approval waits: report their call references and reconcile uncertainty
  before retrying. Keep normal dedicated inspection, editing, and project-test
  tools for coding work."""

WORKFLOW_PROGRAM_GUIDANCE = """\
## Program workflow steps

- Use a `program` step for a reviewed bounded read/filter stage with predictable
  flow; use `run` for judgment and prefer existing direct steps for writes or
  approval-sensitive actions. Provide its required description of what runs and
  why. Declare canonical tool IDs, typed inputs, a required output schema, a
  stopping condition, and optional downward-only limits. Pass data through
  `program.inputs`; never interpolate data or tool output into source.
- Do not include model-invoking memory/knowledge tools or nested agents in a
  workflow program's tool allowlist, or call inference APIs from workflow code.
- Never pass credentials or other secrets as program inputs.
- Chain structured outputs using documented fields. Exactly one validated
  `await cognis.emit(json_value)` becomes `steps.<name>.outputs`; missing
  required data follows the existing `on_error` behavior. A nested approval suspends the same live
  execution and is not success or failure. Each new attempt starts fresh.
- Treat agent-composed code as a candidate. Review and accept the versioned,
  pinned code/configuration before enabling execution or scheduling it. Never
  replay an uncertain attempt or use self-editing code."""

PROGRAM_COMPACTION_GUIDANCE = """\
For Python execution, record the last observed kernel generation, useful
variable names, completed call/output references, and uncertain operations.
Compaction preserves a live kernel, not its durability. Do not describe lost
variables as present; check recovery notices. Never repeat work solely because
its full transcript was compacted."""


def turn_guidance(visible_tool_names: set[str] | frozenset[str] | None) -> str | None:
    """Return operational PTC routing only when the effective tool is exposed."""

    if visible_tool_names is None or PYTHON_EXECUTE_TOOL_NAME not in visible_tool_names:
        return None
    fragments = [PROGRAM_ROUTING_GUIDANCE]
    compaction = compaction_guidance(visible_tool_names)
    if compaction:
        fragments.append(compaction)
    return "\n\n".join(fragments)


def workflow_skill_guidance(
    visible_tool_names: set[str] | frozenset[str] | None,
) -> str | None:
    """Return program authoring rules only when PTC and workflow tooling coexist."""

    if visible_tool_names is None:
        return None
    if PYTHON_EXECUTE_TOOL_NAME not in visible_tool_names:
        return None
    if not ({"create_workflow", "update_workflow"} & set(visible_tool_names)):
        return None
    return WORKFLOW_PROGRAM_GUIDANCE


def compaction_guidance(
    visible_tool_names: set[str] | frozenset[str] | None,
) -> str | None:
    """Return the PTC continuation note only for a PTC-enabled runtime."""

    if visible_tool_names is None or PYTHON_EXECUTE_TOOL_NAME not in visible_tool_names:
        return None
    return PROGRAM_COMPACTION_GUIDANCE
