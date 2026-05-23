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
                "minLength": 1,
                "pattern": "\\S",
                "description": "Brief summary of what was accomplished in this step.",
            },
            "outputs": {
                "type": "object",
                "description": "Structured outputs from this step (key-value pairs).",
            },
            "metadata": {
                "type": "object",
                "description": (
                    "Workflow-step-specific structured metadata. When the current step defines "
                    "a metadata contract, all required fields must be present and must match "
                    "the declared JSON types."
                ),
            },
            "claims": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Claims about what was achieved, for evaluation.",
            },
            "outcome": {
                "type": "object",
                "description": (
                    "Optional business outcome for this step. Use status 'rejected' when the "
                    "step completed properly but the reviewed work should go back for revision, "
                    "or 'failed' when the step itself could not complete successfully."
                ),
                "properties": {
                    "status": {
                        "type": "string",
                        "enum": ["success", "rejected", "failed"],
                        "description": "Outcome status for workflow routing.",
                    },
                    "reason": {
                        "type": "string",
                        "description": "Required for rejected or failed outcomes.",
                    },
                },
                "required": ["status"],
            },
            "notification": {
                "type": "object",
                "description": (
                    "Optional completion delivery choice. Use 'silent' only when silent completion "
                    "is explicitly allowed and nothing user-actionable happened. Use 'direct' for "
                    "ready-to-read outputs like daily briefs or summaries when the result should be "
                    "sent directly to the resolved target channel."
                ),
                "properties": {
                    "mode": {
                        "type": "string",
                        "enum": ["silent", "direct"],
                        "description": (
                            "Request silent completion with no outward notification, or direct "
                            "delivery to the resolved target channel."
                        ),
                    },
                    "reason": {
                        "type": "string",
                        "description": "Required for silent completion. Optional for direct.",
                    },
                },
                "required": ["mode"],
            },
        },
        "required": ["summary"],
    },
    source=_SOURCE,
    category="workflow",
    read_only=False,
)

WRITE_DELIVERABLE_TOOL = ToolDefinition(
    name="write_deliverable",
    description=(
        "Write the user-facing deliverable for this workflow step. Call this before "
        "step_complete when the step requires a deliverable. The content you pass is "
        "the canonical workflow artifact and replaces any free-text response as the "
        "step's user-facing output."
    ),
    parameters={
        "type": "object",
        "properties": {
            "content": {
                "type": "string",
                "description": "The final deliverable content for this step.",
            },
            "format": {
                "type": "string",
                "enum": ["markdown", "plain", "html"],
                "description": "How the deliverable should be rendered.",
                "default": "markdown",
            },
            "title": {
                "type": "string",
                "description": "Optional title for the deliverable.",
            },
            "target": {
                "type": "string",
                "enum": ["channel", "none"],
                "description": (
                    "Optional delivery hint. Final workflow policy decides what actually "
                    "gets delivered."
                ),
            },
            "outputs": {
                "type": "object",
                "description": "Optional structured sidecar data for evaluators or later steps.",
            },
        },
        "required": ["content"],
    },
    source=_SOURCE,
    category="deliverable",
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
        "Track progress within this step. Use todos for multi-step work, "
        "break complex tasks into concrete actionable items, keep only one "
        "item in progress at a time, and mark items completed or cancelled "
        "as soon as their status changes. Todos survive compaction and help "
        "maintain context across long conversations."
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


# Stage 36: switch_executor — controller-handled tool that changes the
# conversation's active executor for subsequent executor-routed tool calls.
# The active executor binding persists across turns and steps until the
# next switch (by the agent or by the user via /executor). The controller
# never auto-changes it; this tool is the agent's only mutator.
SWITCH_EXECUTOR_TOOL = ToolDefinition(
    name="switch_executor",
    description=(
        "Change the active executor for subsequent tool calls in this conversation. "
        "Use when you want to keep working on a different assigned executor without "
        "specifying target_executor on every call. The target executor must be one "
        "of the executors assigned to you (primary or additional) and currently "
        "usable. Use primary executors for normal work. Additional executors are "
        "special-purpose targets and must not be used merely as fallback capacity "
        "when a primary executor is down; switch to one only when the task requires "
        "that specific machine or the user asks for it. Switch back to a primary "
        "executor after that specific work is done, and before unrelated or generic "
        "follow-up work. Switching to a non-primary (additional) executor will be "
        "flagged in your context until you switch back to a primary."
    ),
    parameters={
        "type": "object",
        "properties": {
            "executor_id": {
                "type": "string",
                "minLength": 1,
                "description": (
                    "Assigned executor id to make active. Must be one of the agent's "
                    "primary or additional executors. Additional executors are "
                    "special-purpose targets, not general fallback capacity."
                ),
            },
            "reason": {
                "type": "string",
                "description": "Brief, optional reason for the switch.",
            },
        },
        "required": ["executor_id"],
    },
    source=_SOURCE,
    category="workflow",
    read_only=False,
)


def workflow_tools() -> list[ToolDefinition]:
    """Return built-in workflow tool definitions.

    These are display-only definitions for the tool registry.
    Actual handling is done by the agent loop, not the tool router.
    """
    return [
        WRITE_DELIVERABLE_TOOL,
        STEP_COMPLETE_TOOL,
        STEP_REQUEST_INPUT_TOOL,
        REQUEST_CREDENTIAL_TOOL,
        REQUEST_AUTH_CHALLENGE_TOOL,
        LIST_CREDENTIALS_TOOL,
        STEP_TODO_WRITE_TOOL,
        STEP_TODO_LIST_TOOL,
        SWITCH_EXECUTOR_TOOL,
    ]
