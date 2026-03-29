"""Built-in workflow tool definitions.

These tools are controller-injected into the LLM prompt by the agent loop.
They are NOT dispatched through the executor or tool router. The definitions
here exist solely for visibility on the Tools page and tool registry.
"""

from __future__ import annotations

from cognis.models.tool import ToolDefinition, ToolSource

_SOURCE = ToolSource(type="builtin")

STEP_COMPLETE_TOOL = ToolDefinition(
    name="step_complete",
    description=(
        "Signal that this workflow step is complete. "
        "Call this when the step objective is satisfied."
    ),
    parameters={
        "type": "object",
        "properties": {
            "summary": {
                "type": "string",
                "description": "Brief summary of what was accomplished in this step.",
            },
            "outputs": {
                "type": "object",
                "description": "Structured outputs from this step (key-value pairs).",
            },
            "claims": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Claims about what was achieved, for evaluation.",
            },
        },
        "required": ["summary"],
    },
    source=_SOURCE,
    category="workflow",
    read_only=False,
)

STEP_REQUEST_INPUT_TOOL = ToolDefinition(
    name="step_request_input",
    description=(
        "Request input from the caller while staying in the same step. "
        "Use when you need clarification or a decision before proceeding."
    ),
    parameters={
        "type": "object",
        "properties": {
            "question": {
                "type": "string",
                "description": "The question to ask the caller.",
            },
            "options": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional list of suggested answers.",
            },
            "context": {
                "type": "string",
                "description": "Background context for the question.",
            },
        },
        "required": ["question"],
    },
    source=_SOURCE,
    category="workflow",
    read_only=False,
)

STEP_TODO_WRITE_TOOL = ToolDefinition(
    name="step_todo_write",
    description=(
        "Track progress within this step. Todos survive compaction and "
        "help maintain context across long conversations."
    ),
    parameters={
        "type": "object",
        "properties": {
            "todos": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "content": {
                            "type": "string",
                            "description": "Brief description of the task.",
                        },
                        "status": {
                            "type": "string",
                            "enum": ["pending", "in_progress", "completed", "cancelled"],
                            "description": "Current status of the task.",
                        },
                    },
                    "required": ["content", "status"],
                },
                "description": "The updated todo list.",
            },
        },
        "required": ["todos"],
    },
    source=_SOURCE,
    category="workflow",
    read_only=False,
)

STEP_TODO_LIST_TOOL = ToolDefinition(
    name="step_todo_list",
    description="Read current step todos.",
    parameters={"type": "object", "properties": {}},
    source=_SOURCE,
    category="workflow",
    read_only=True,
)


def workflow_tools() -> list[ToolDefinition]:
    """Return built-in workflow tool definitions.

    These are display-only definitions for the tool registry.
    Actual handling is done by the agent loop, not the tool router.
    """
    return [
        STEP_COMPLETE_TOOL,
        STEP_REQUEST_INPUT_TOOL,
        STEP_TODO_WRITE_TOOL,
        STEP_TODO_LIST_TOOL,
    ]
