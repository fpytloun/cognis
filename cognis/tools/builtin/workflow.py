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

REQUEST_CREDENTIAL_TOOL = ToolDefinition(
    name="request_credential",
    description=(
        "Request a durable credential from the user without exposing the secret value to the LLM."
    ),
    parameters={
        "type": "object",
        "properties": {
            "credential_id": {"type": "string", "description": "Credential ID to create/update"},
            "kind": {"type": "string", "description": "Credential kind"},
            "label": {"type": "string", "description": "Human-readable credential label"},
            "description": {"type": "string", "description": "Why this credential is needed"},
            "metadata": {
                "type": "object",
                "description": "Non-secret metadata such as login_url or domain",
            },
            "required_fields": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Expected payload fields such as username/password/token",
            },
            "agent_id": {"type": "string", "description": "Optional agent scope override"},
            "scope": {"type": "string", "description": "Credential scope (default: user)"},
        },
        "required": ["credential_id", "kind", "label"],
    },
    source=_SOURCE,
    category="workflow",
    read_only=False,
)

REQUEST_AUTH_CHALLENGE_TOOL = ToolDefinition(
    name="request_auth_challenge",
    description=(
        "Request a live auth or MFA challenge response from the user without exposing the value to the LLM."
    ),
    parameters={
        "type": "object",
        "properties": {
            "kind": {
                "type": "string",
                "description": "Challenge kind such as otp_code or push_approval",
            },
            "label": {"type": "string", "description": "Short title shown to the user"},
            "message": {"type": "string", "description": "What the user should do"},
            "metadata": {
                "type": "object",
                "description": "Safe non-secret context such as origin/domain/login_url",
            },
            "required_fields": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Expected response fields, e.g. code",
            },
            "timeout_seconds": {"type": "integer", "description": "Challenge timeout in seconds"},
        },
        "required": ["kind", "label", "message"],
    },
    source=_SOURCE,
    category="workflow",
    read_only=False,
)

LIST_CREDENTIALS_TOOL = ToolDefinition(
    name="list_credentials",
    description=(
        "List credential metadata that the current agent is allowed to use. "
        "Use this to discover available credential IDs before requesting or using one."
    ),
    parameters={
        "type": "object",
        "properties": {
            "kind": {"type": "string", "description": "Optional credential kind filter"},
            "domain": {"type": "string", "description": "Optional domain filter"},
            "origin": {"type": "string", "description": "Optional origin filter"},
            "label_contains": {
                "type": "string",
                "description": "Optional case-insensitive label filter",
            },
        },
    },
    source=_SOURCE,
    category="workflow",
    read_only=True,
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
        REQUEST_CREDENTIAL_TOOL,
        REQUEST_AUTH_CHALLENGE_TOOL,
        LIST_CREDENTIALS_TOOL,
        STEP_TODO_WRITE_TOOL,
        STEP_TODO_LIST_TOOL,
    ]
