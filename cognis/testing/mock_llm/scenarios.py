"""Scenario management for the mock LLM server.

Scenarios are YAML files describing the exact streaming sequence the mock
server will emit in response to a trigger message.  The scenario catalog
is loaded from a directory at startup and can be updated at runtime via
the control-plane API.

Scenario format
---------------
id: thinking-multiblock
description: "Thinking segment grows a second block mid-stream"
trigger: "scenario:thinking-multiblock"
turns:
  - role: assistant
    steps:
      - type: thinking
        block_id: blk_1
        title: "Thinking"
        content: "Step 1 reasoning"
        complete: false
        delay_ms: 50
      - type: text
        chunks: ["Here ", "is ", "my ", "answer."]
        delay_ms: 30
      - type: tool_call
        call_id: call_1
        name: bash
        arguments: {"command": "ls -la"}
        delay_ms: 10

Step types
----------
thinking  — emit a thinking block (may be incomplete, triggering streaming)
text      — emit text chunks (streaming assistant message)
tool_call — emit a tool call (triggers phase bump)
tool_result — emit a tool result (for the previous tool_call)
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Any

try:
    import yaml
    _YAML_AVAILABLE = True
except ImportError:
    _YAML_AVAILABLE = False

logger = logging.getLogger(__name__)


class ScenarioCatalog:
    """Thread-safe catalog of scenario scripts.

    Scenarios are keyed by their trigger string (last user message marker)
    or by their id.  The active scenario can be overridden at runtime via
    the control-plane API.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._by_trigger: dict[str, dict[str, Any]] = {}
        self._by_id: dict[str, dict[str, Any]] = {}
        self._active_id: str | None = None
        self._history: list[dict[str, Any]] = []
        self._max_history = 50

    # ------------------------------------------------------------------
    # Loading
    # ------------------------------------------------------------------

    def load_directory(self, path: Path) -> int:
        """Load all *.yaml scenario files from a directory.

        Returns the number of scenarios loaded.
        """
        if not _YAML_AVAILABLE:
            logger.warning("PyYAML not available — scenario files cannot be loaded")
            return 0
        if not path.exists():
            logger.warning("Scenarios directory does not exist: %s", path)
            return 0

        count = 0
        for yaml_file in sorted(path.glob("*.yaml")):
            try:
                scenario = yaml.safe_load(yaml_file.read_text())
                if isinstance(scenario, dict) and scenario.get("id"):
                    self.upsert(scenario)
                    count += 1
            except Exception:
                logger.exception("Failed to load scenario from %s", yaml_file)
        logger.info("Loaded %d scenarios from %s", count, path)
        return count

    def upsert(self, scenario: dict[str, Any]) -> None:
        """Add or replace a scenario."""
        scenario_id = scenario.get("id", "")
        trigger = scenario.get("trigger", "")
        with self._lock:
            self._by_id[scenario_id] = scenario
            if trigger:
                self._by_trigger[trigger] = scenario

    # ------------------------------------------------------------------
    # Lookup
    # ------------------------------------------------------------------

    def resolve(self, user_message: str) -> dict[str, Any] | None:
        """Return the scenario for a given user message.

        Priority:
        1. Active scenario override (set via /__mock/active).
        2. Trigger match (last user message marker).
        3. None (no scenario — return a default empty response).
        """
        with self._lock:
            if self._active_id is not None:
                scenario = self._by_id.get(self._active_id)
                if scenario:
                    return scenario

            # Try trigger match
            for trigger, scenario in self._by_trigger.items():
                if trigger in user_message:
                    return scenario

        return None

    def set_active(self, scenario_id: str | None) -> bool:
        """Set the active scenario override.  Pass None to clear."""
        with self._lock:
            if scenario_id is not None and scenario_id not in self._by_id:
                return False
            self._active_id = scenario_id
        return True

    def list_scenarios(self) -> list[dict[str, Any]]:
        """Return summary of all loaded scenarios."""
        with self._lock:
            return [
                {
                    "id": s.get("id"),
                    "description": s.get("description", ""),
                    "trigger": s.get("trigger", ""),
                    "active": s.get("id") == self._active_id,
                }
                for s in self._by_id.values()
            ]

    # ------------------------------------------------------------------
    # History (for interactive debugging)
    # ------------------------------------------------------------------

    def record_request(self, request: dict[str, Any], scenario_id: str | None) -> None:
        with self._lock:
            self._history.append({"request": request, "scenario_id": scenario_id})
            if len(self._history) > self._max_history:
                self._history = self._history[-self._max_history:]

    def get_history(self, limit: int = 10) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._history[-limit:])

    def clear_history(self) -> None:
        with self._lock:
            self._history.clear()


# ---------------------------------------------------------------------------
# Scenario rendering — convert a scenario step into SSE chunks
# ---------------------------------------------------------------------------

def _turn_ends_with_tool_call(turn: dict[str, Any]) -> bool:
    """Return True if the turn's last non-empty step is a tool_call.

    When True the stream must end with finish_reason="tool_calls" so the agent
    loop executes the tool and re-invokes the LLM (advancing assistant_phase_index).
    When False (text-final turn) the stream ends with finish_reason="stop".
    """
    steps = [s for s in turn.get("steps", []) if s.get("type")]
    if not steps:
        return False
    return steps[-1].get("type") == "tool_call"


def render_chat_completion_stream(
    scenario: dict[str, Any],
    model: str = "mock-model",
    request_id: str = "chatcmpl-mock",
    turn_index: int = 0,
) -> list[str]:
    """Convert a scenario into a list of SSE data lines for chat completions.

    For multi-turn scenarios (multiple assistant turns), ``turn_index`` selects
    which turn to render.  The agent loop calls the LLM once per turn:
    - turn 0: initial response (may include tool calls → finish_reason="tool_calls")
    - turn 1: response after tool results (text → finish_reason="stop")
    - etc.

    The finish_reason is derived from the turn content:
    - Last step is tool_call → "tool_calls" (agent loop executes tool, re-invokes LLM)
    - Last step is text/thinking → "stop" (turn complete)

    This is critical for multi-phase turns: without "tool_calls", the agent loop
    treats the turn as finished after the first response and never bumps
    assistant_phase_index, so the production multi-phase pattern is never reproduced.

    Returns a list of ``data: {...}`` strings (without the trailing newlines).
    The caller is responsible for streaming them with appropriate delays.
    """
    import json
    import time

    chunks: list[str] = []
    all_turns = scenario.get("turns", [])
    # Select only assistant turns
    assistant_turns = [t for t in all_turns if t.get("role") == "assistant"]
    # Pick the right turn; fall back to last if index exceeds
    if not assistant_turns:
        return chunks
    active_turn = assistant_turns[min(turn_index, len(assistant_turns) - 1)]

    # Determine finish_reason before emitting chunks
    ends_with_tool = _turn_ends_with_tool_call(active_turn)
    finish_reason = "tool_calls" if ends_with_tool else "stop"

    # Track tool_call index for multi-tool turns (each tool_call needs a unique index)
    tool_call_index = 0

    steps = active_turn.get("steps", [])
    for step in steps:
        step_type = step.get("type", "text")

        if step_type == "text":
            text_chunks = step.get("chunks", [step.get("content", "")])
            for chunk_text in text_chunks:
                delta = {
                    "id": request_id,
                    "object": "chat.completion.chunk",
                    "created": int(time.time()),
                    "model": model,
                    "choices": [{
                        "index": 0,
                        "delta": {"role": "assistant", "content": chunk_text},
                        "finish_reason": None,
                    }],
                }
                chunks.append(f"data: {json.dumps(delta)}")

        elif step_type == "thinking":
            # Emit as reasoning_content — the field Cognis's StreamAccumulator
            # reads from delta.get("reasoning_content") to trigger on_thinking.
            # Emit the content in small chunks to simulate real streaming.
            content = step.get("content", "")
            # Split into ~20-char chunks to simulate token-by-token streaming
            chunk_size = 20
            content_chunks = [content[i:i+chunk_size] for i in range(0, len(content), chunk_size)] or [""]
            for chunk_text in content_chunks:
                delta = {
                    "id": request_id,
                    "object": "chat.completion.chunk",
                    "created": int(time.time()),
                    "model": model,
                    "choices": [{
                        "index": 0,
                        "delta": {
                            "role": "assistant",
                            "content": "",
                            "reasoning_content": chunk_text,
                        },
                        "finish_reason": None,
                    }],
                }
                chunks.append(f"data: {json.dumps(delta)}")

        elif step_type == "tool_call":
            call_id = step.get("call_id", "call_mock")
            name = step.get("name", "unknown")
            arguments = step.get("arguments", {})
            delta = {
                "id": request_id,
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": model,
                "choices": [{
                    "index": 0,
                    "delta": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [{
                            "index": tool_call_index,
                            "id": call_id,
                            "type": "function",
                            "function": {
                                "name": name,
                                "arguments": json.dumps(arguments),
                            },
                        }],
                    },
                    "finish_reason": None,
                }],
            }
            chunks.append(f"data: {json.dumps(delta)}")
            tool_call_index += 1

    # Final stop/tool_calls chunk — correct finish_reason drives agent loop behaviour
    import time as _time
    stop_delta = {
        "id": request_id,
        "object": "chat.completion.chunk",
        "created": int(_time.time()),
        "model": model,
        "choices": [{"index": 0, "delta": {}, "finish_reason": finish_reason}],
    }
    chunks.append(f"data: {json.dumps(stop_delta)}")
    chunks.append("data: [DONE]")

    return chunks


def get_step_delays(scenario: dict[str, Any], turn_index: int = 0) -> list[float]:
    """Return per-chunk delay in seconds for each step in the active turn."""
    delays: list[float] = []
    all_turns = scenario.get("turns", [])
    assistant_turns = [t for t in all_turns if t.get("role") == "assistant"]
    if not assistant_turns:
        return delays
    active_turn = assistant_turns[min(turn_index, len(assistant_turns) - 1)]
    for turn in [active_turn]:
        if turn.get("role") != "assistant":
            continue
        for step in turn.get("steps", []):
            step_type = step.get("type", "text")
            delay_ms = step.get("delay_ms", 30)
            if step_type == "text":
                n_chunks = len(step.get("chunks", [step.get("content", "")]))
                delays.extend([delay_ms / 1000.0] * n_chunks)
            else:
                delays.append(delay_ms / 1000.0)
    delays.append(0.0)  # stop chunk
    delays.append(0.0)  # [DONE]
    return delays


def build_default_response(model: str = "mock-model") -> dict[str, Any]:
    """Return a minimal non-streaming chat completion for unknown scenarios."""
    import time
    return {
        "id": "chatcmpl-mock-default",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{
            "index": 0,
            "message": {
                "role": "assistant",
                "content": "Mock response — no scenario matched.",
            },
            "finish_reason": "stop",
        }],
        "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20},
    }
