"""Portable runtime guide for executor-supervised Python programs.

The wording adapts patterns from Prime Intellect's ``prime-agent`` P1
``core.md``, ``ipython.rs``, and ``usage.md`` (MIT, revision
``8aac8692456c00c01d4e53e60e6d3347a3506559``); ``nano-rlm`` N1–N3
``prompt.py``, ``tools/ipython.py``, and ``compaction.py`` (MIT, revision
``84110bcb73e3c1acaf69047fda50aa4dff15eea1``); and ``smolagents`` S1
``code_agent.yaml`` (Apache-2.0, revision
``12c1bc820eca50ace6f80a21d90426d41d74f845``), plus the OpenAI PTC guide at
``https://developers.openai.com/api/docs/guides/tools-programmatic-tool-calling``
(retrieved 2026-10-22). These are adaptations, not quotations.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from cognis import json_codec as json

_RUNTIME_GUIDE_PREAMBLE = """\
`cognis` is pre-imported. Tool wrappers are async: use top-level `await`.
Variables survive cells and compaction only while this kernel generation lives.
No tasks may outlive a cell.

Each cell has its own description of what it will do and why. Helpers and tool
wrappers retained from earlier cells use the current execution context when
invoked; do not supply or override caller context in tool arguments.

Use the shown signatures. Discover deferred tools with the runtime's documented
search/describe/load API, and do not guess names or fields. Use `.data` only for
declared validated structured output; otherwise inspect `.text`. Check
truncation and preserve call/recovery/source references. Stay within displayed
concurrency, retry, time, and output limits.

Finish with `await cognis.emit(json_value)`. Printing is diagnostic, not completion.
An approval suspends the affected await and resumes normally; do not repeat it.
A denied call did not execute; do not bypass it or claim its result. Do not claim
completion while required calls or effects are unresolved. After cancellation or
state loss, reconcile recorded calls and reconstruct variables. Never replay an
interrupted cell blindly.

The runtime plan below is authoritative for this execution: it supplies the
generation, executor policy, imports, limits, eligible tool descriptors,
discovery availability, and exception types. The plan does not promise a
broader Cognis tool catalog, agent invocations, or direct MCP clients. Python is
not a sandbox; ordinary standard-library, filesystem, network, and process
capabilities depend on executor policy. Do not use them to bypass tool policy."""


def render_runtime_guide(
    *,
    generation: str,
    executor_policy: Mapping[str, Any],
    imports: Sequence[str],
    limits: Mapping[str, Any],
    eligible_tools: Sequence[Mapping[str, Any]],
    discovery: Mapping[str, Any],
    exception_types: Sequence[str],
) -> str:
    """Render a guide from the exact effective plan enforced by the runtime."""

    contract = {
        "generation": generation,
        "executor_policy": dict(executor_policy),
        "imports": list(imports),
        "limits": dict(limits),
        "eligible_tools": [dict(tool) for tool in eligible_tools],
        "discovery": dict(discovery),
        "exception_types": list(exception_types),
    }
    return f"{_RUNTIME_GUIDE_PREAMBLE}\n\nEffective runtime contract:\n{json.dumps(contract, ensure_ascii=False, indent=2, sort_keys=True)}"
