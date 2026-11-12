# Cognis: Tool System

## Overview

Tools are how agents interact with the world. The controller's **Tool Router**
categorizes each tool call and routes it to the appropriate handler. The
executor handles all actual tool execution — the controller never runs tools.

For runtime-backed agents, tool handling is part of the runtime contract. See
`18-runtime-contract.md` for the shared policy model and runtime translation
rules.

## Tool Sources

### Instance configuration management

`manage_providers`, `manage_routes`, `manage_settings`, and `manage_executors`
declare canonical action schemas in `cognis/tools/builtin/system_management.py`.
Descriptions, validation, plan-mode classification, and policy metadata derive
from those operation descriptors. The router accepts only registered builtin
sources, requires explicit agent opt-in at execution, and always evaluates these
non-bypassable operations. The handler checks root owner primary context and
loads the trusted user's current role; arguments cannot supply actor identity.

Transport-neutral management services own resource authorization, validation,
persistence, and runtime effects. REST routes and tools share these services.
Tool results are redacted and then pass the normal untrusted-result boundary.
Provider and executor writes serialize with the existing database dispatch guard;
timestamp preconditions are checked within that transaction. Named routes retain
their store revision CAS. Task bindings and workflow profiles use locked aggregate
snapshots and generation-bearing revision hashes to prevent absent-row ABA.

Configuration previews execute validation and dependency checks but neither commit
nor run runtime effects. Mutations preserve omitted fields, replace supplied config
documents, and never cascade dependencies. Explicit create IDs support read-before-
retry reconciliation; stale updates must be read and reviewed again. Provider cache
invalidation and executor fencing/reconfiguration run after commit. Web mutations
finish propagation cancellation-safely; hot settings conditionally recover only
their own committed revision. The supported REST payload/response contracts remain
unchanged. These are application configuration operations, not executor tool
execution or arbitrary administrative shell access.

Each domain has a bundled management skill. QuickStart explicitly enables all six
instance-management opt-ins (including existing agent and MCP tools) and attaches
available management skills, with an advanced opt-out. Existing agents retain
their current permissions until explicitly configured.

### Owner-scoped credential assignment

MCP `credential_bearer` authentication binds one existing `.token` credential
reference to one owner agent and a private HTTPS MCP destination. Resolution uses
the canonical credential provider. Controller dispatch checks current durable
grants, owner, private WebSocket executor assignment and credential version;
executor dispatch checks the matching client generation. Discovery applies the
same agent binding. Changed configurations require reconfiguration, never reuse
a stale cached bearer. Static secret and OAuth contracts are unchanged.

`manage_executors.provision_token` atomically persists the executor token version,
encrypted agent-scoped credential and caller grant in one transaction. It uses
the existing signer and post-commit fence. An operation ID stored with the
credential reconciles retries, including a pending fence after cancellation.
No secret CRUD, HTTP impersonation, signing path or database schema is added.

`manage_agents` declares `credentials_get` (read) and `credentials_grant` /
`credentials_revoke` (update) in its canonical native operation registry.
All three require an exact `agent_id` and `credential_id`; mutations additionally
require the agent's `expected_updated_at`. Only root owner primary chats can use
this tool. Both resources are owner-checked, without administrator bypass.

Credential metadata is read through the configured provider and projected onto
identity/policy fields only. Assignment mutations use agent revision CAS in one
transaction, changing only `permissions.allowed_credentials`. A stale write is
rejected, including a concurrent no-op. Cancellation before commit rolls back;
retry requires a fresh revision. There is no credential payload mutation,
secret resolution, agent activation, or sharing side effect.

Grant validates status, expiry and agent scope; revoke only removes an assignment.
The credential provider continues to enforce current credential scope and status
at resolution. Agent permissions follow the existing runtime-definition refresh
lifecycle, so revoking an assignment is not cancellation of already-running work.

Tools come from five sources, each with a priority for deduplication:

| Source | Priority | Description |
|--------|----------|-------------|
| `builtin` | 500 | Controller-side tools (orchestration, system, workflow directives) |
| `executor` | 400 | Executor-native tools (filesystem, search, shell, web, and gated Python PTC). |
| `skill` | 300 | Skill-provided tools (DB-managed instruction + tool bundles) |
| `local_mcp` | 200 | Per-agent MCP servers running on the executor |
| `intaris_mcp` | 100 | Remote MCP servers proxied through Intaris |

When the same tool name appears from multiple sources, the higher-priority
source wins. Same-source duplicates are configuration errors.

## Tool Categories and Routing

```
LLM returns tool calls
  │
  ├─ Orchestration (delegate, task/workflow control)
  │    → Controller handles as session management operation
  │    → Not "tool execution" — these are orchestration directives
  │
  ├─ Intaris-managed MCP (github/, slack/, remote APIs)
  │    → Controller calls Intaris MCP proxy
  │    → Intaris evaluates + executes in one call
  │    → Executor NOT involved
  │
  ├─ Executor-native (read, write, edit, bash, glob, grep, Python PTC, etc.)
  │    → Controller evaluates via Intaris
  │    → If approved: Controller sends tool.execute to Executor
  │    → Executor runs native handler, returns result
  │    → PTC nested tool calls return through the controller's normal router
  │
  └─ Local MCP (per-agent MCP servers)
       → Controller evaluates via Intaris
       → If approved: Controller sends tool.execute to Executor
       → Executor dispatches to MCP server, returns result
```

### Programmatic tool calling (PTC)

The executor-native PTC tools are `python_execute`,
`python_kernel_status`, and `python_kernel_reset`. `python_execute` requires a
non-empty `description` and literal `code`; optional `inputs`,
`input_schema`, `output_schema`, and `limits` are passed as separate arguments.
Workflow source is pinned and never template-rendered; workflow input and the
single emitted result are schema-validated. See
[`34-deterministic-workflows.md`](34-deterministic-workflows.md) for the
workflow-specific contract and
[`tools-and-skills.md`](../guide/tools-and-skills.md#programmatic-tool-calling-ptc)
for the interactive argument example.

`python_execute` and `python_kernel_reset` are non-bypassable. The required
description is bound to the parent program invocation, and Intaris evaluates it
through the ordinary policy/approval path; child calls are evaluated separately.
A pending child approval suspends the same cell and does not consume its active
wall-time budget.

PTC visibility is gated by executor configuration in
`cognis/core/program_config.py`: a configured executor whose
`labels.cognis_runtime_profile` is exactly `pod` is eligible by default, unless
`config.program_runtime.enabled` is `false`. All other identities (including
WebSocket or unknown executors not labelled `pod`) require
`config.program_runtime.host_enabled: true`. The host opt-in does not override
an explicit `enabled: false`.

Each call receives a controller-bound scope, generation, description, executor
identity, and effective tool plan. Interactive scope is the conversation ID, or
session ID if no conversation ID exists; on the same executor its IPython
namespace persists across cells while the kernel remains alive. State is
volatile, not an on-disk heap:
the local ledger stores recovery metadata, not variables. Kernel state can be
lost on executor restart, idle reclamation, or terminal cleanup. The default
idle TTL is 30 minutes and the executor caps live kernels at 64. Workflow PTC
uses the current fresh `StepRun` ID and discards the kernel at the step boundary;
each new run starts fresh.

`python_kernel_status` takes no arguments and reports `state`,
`kernel_generation`, and available `state_lost_reason`, not namespace values.
`python_kernel_reset` requires the current
`expected_generation`, only resets an idle kernel, and advances its generation.
Subsequent `python_execute` calls in an existing scope must supply the current
`expected_generation`. On state loss or an uncertain outcome, inspect
`state_lost_reason` and `child_call_references`, then reconcile effects before
retry; execution is not blindly replayed.

Nested calls are restricted to the current effective plan.
`await cognis.tools.search(query, limit=10)` searches that plan;
`await cognis.tools.describe(tool_id)` returns one descriptor; and
`cognis.tools.load(descriptor)` creates an async callable for a deferred member
of that plan. They do not expose a broader catalog. Each child call is
independently evaluated through `ToolRouter` and normal Intaris policy and
approval; parent approval is not inherited. Writable tools are eligible unless
an explicit session policy requires read-only access. Workflow programs remain
read-only, apply their explicit canonical-ID allowlist, and exclude
model-invoking, recursive, management, and unsupported-source tools. When a
plan includes writable tools, child calls are serialized in v1. The generated
`cognis.runtime_guide` is the per-call authority for exact aliases, schemas,
executor policy, limits, and discovery.

Forwarded program RPC uses the authenticated controller bridge capability
`program_rpc_v1`, with at most 64 active reverse child calls. The controller
owning the live invocation performs authorization and approval; the physical
executor owner only relays the call and status/cancel messages. Without the
capability, Python execution is rejected before dispatch. Disconnect does not
replay an accepted/unknown cell or child effect. Pending approval/child work is
interrupted when trusted continuation cannot be retained; uncertain outcomes
block subsequent governed child dispatch until reconciliation. Existing
same-instance accepted-call recovery may retrieve a retained terminal result.
A live heap and its bridge/continuation are separate: an idle remote kernel may
survive a controller disconnect, while an in-flight child continuation may
not. Executor process restart or local provider shutdown closes kernels and
advances generation; no heap restoration is attempted.

The Python kernel is not a sandbox and nested tool governance does not constrain
direct Python operations. Code can use the executor user's accessible
filesystem, credentials, mounts, network, and processes available to that user.
Keep secrets out of source and inputs. Use normal Intaris evaluation for
approval-sensitive nested mutations.

The Chat v2 timeline uses the existing real-time tool-call cards: a Python parent
card discloses highlighted source, readable output/result, generation, and linked
child tool cards without duplicating the generic input/result body. Python uses
a separate code activity group. Flushed stdout/stderr retain IOPub ordering and
stream identity through the ordinary bounded tool-progress spool, reconnect, and
Chat v2 runtime overlay; terminal previews come from the canonical program result.
No Python-specific output spool or replay mechanism is introduced. Protocol
metadata is behind an explicit raw-data disclosure. A child awaiting approval
remains actionable and marks the parent as
waiting; contiguous linked events are grouped under the parent rather than
duplicated at top level. The live acceptance harness distinguishes end-to-end
execution from synthetic timeline coverage.
`tests/e2e/test_ptc_live_qualification.py` exercises real Cognis, Intaris,
Mnemory, and IPython with a deterministic model/policy provider, including
separately approved nested mutations. The canonical browser acceptance in
`tests/e2e/test_ptc_live_browser_acceptance.py` verifies parent/child ordering,
stable identity, reconnect/reload deduplication, and persistent second-cell
state. Synthetic timeline fixtures alone do not prove live backend execution.

The runtime guide renderer and upstream-adaptation provenance remain owned by
`cognis/core/program_guidance.py`; this contract intentionally does not duplicate
that prompt text.

Not shipped in this PTC slice: recursive agent bindings or vendor-specific PTC
adapters, kernel-heap restore, notebook UI, or production A/B evaluation.

## Tool Definition

```python
class ToolDefinition(BaseModel):
    name: str                          # Unique identifier
    description: str                   # For LLM tool selection
    parameters: dict                   # JSON Schema
    source: ToolSource
    category: str = "general"
    read_only: bool = False
    requires_secrets: list[str] = []
    timeout_seconds: int = 30
    non_bypassable: bool = False       # If true, ALWAYS goes through guardrails
    max_result_size: int = 50_000
    content_trust: Literal["trusted", "untrusted"] = "trusted"
    risk_level: str | None = None
    aliases: list[str] = []
    configurable: bool = True
    surfaces: list[str] = []

class ToolSource(BaseModel):
    type: str                          # "builtin", "executor", "local_mcp", "intaris_mcp", "skill"
    server_name: str | None = None     # MCP server display name
    server_id: str | None = None       # MCP server stable ID (for stable_tool_id)
    raw_tool_name: str | None = None   # Original MCP tool name (for dispatch)
    skill_id: str | None = None
```

### Authoritative operation descriptors

Native tools (`builtin`, `controller`, and `executor`) are classified by their
shared contract before registration or serialization. A declared category maps
to an explicit profile group, unless the owner supplies a group directly.
`read_only` supplies the baseline `read` or `write` capability; owners declare
privileged and destructive capabilities explicitly. Declared `DELETE`
operations also add `destructive`. Capability flags do not derive from tool
names, descriptions, schema prose, or `non_bypassable`.

Native classification is `ready`, `declared`, and confidence `1.0`.
Registration audits and inventory resolution reject missing or inconsistent
native classification instead of invoking a heuristic or an LLM. New native
categories must extend the shared category mapping or supply an explicit group.
Executor RPC payloads use the same normalization, including older payloads
that omit classification fields. Dynamic classification and its persisted
overrides remain limited to external MCP and executable skill sources.

Native tools declare one or more `NativeToolOperation` objects. These
declarations—not a shared schema, action-name heuristic, or annotation—are the
authority for operation schemas, mutation kind, omitted/null/array/concurrency
semantics, dynamic options, examples, side effects, and domain validator IDs.

`ToolDefinition.parameters`, the model-facing function schema, runtime
validation schema, and `ToolDescriptor` are deterministically derived from the
operations. A single operation exposes its schema directly. A multiplexed tool
exposes a `oneOf` union whose branches require `action` with the matching
constant. Native construction rejects missing declarations, duplicate
operations, discriminator mismatches, and contradictory read-only policy.

Simple tools use `NativeToolDefinition`, which creates one
`NativeToolOperation` from the explicitly declared schema and execution policy.
Complex tools such as `manage_agents` and `manage_schedules` declare every
operation directly. The descriptor contract is `cognis.tool.v2`.

MCP/skill/external tools use `authority="external"` and receive an advisory
baseline descriptor from live `inputSchema`, annotations, and optional
`outputSchema`. MCP `readOnlyHint` and related annotations never change Cognis
execution policy or mutation classification.

`describe_tool` resolves callable names or stable tool IDs only inside the
current session's effective authorization-filtered inventory.
For single-operation tools, omit `operation`. If a caller supplies a different
operation name, description returns the sole authoritative operation and
explicit `requested_operation` / `resolved_operation` correction metadata.
This is read-only schema recovery, not an execution alias. Multi-operation
tools still require an exact operation name, or omission for the full descriptor.

`call_tool` requires a `tool` identifier and an `arguments` object. The envelope
is validated against its canonical schema before target resolution; malformed
envelopes return `invalid_tool_arguments` with correction guidance, not
`tool_not_available`. Extra top-level target parameters are rejected rather
than silently discarded. Target authorization and normal execution validation
remain unchanged.

`validate_tool_call` preflights proposed arguments against the same schema and
registered domain validators without execution. Normal controller and executor
execution uses the same contracts. Remote execution also compares controller
and executor schema hashes and fails closed on skew. Neither introspection
operation can reveal tools hidden by permissions, step profiles, source
configuration, or runtime availability.

WS5 removes ad-hoc discovery operations replaced by this contract:

- `manage_agents`: `settings_schema`, `tools_list_available`, and
  `tools_validate`;
- `manage_schedules`: `options`.

State/resource operations remain: `settings_get/update`,
`tools_get/set/add/remove`, schedule `list/get/status`, and schedule mutations.
Settings validation covers supported fields and live provider/model/workflow
domains. Tool options and assignment validation use the caller's actual
effective assignable inventory; unavailable tools and partially unavailable
groups fail closed, preventing a restricted primary agent from expanding
privileges.

`write_deliverable` declares text, generic Rich, dashboard, and Pulse
`NativeToolOperation` entries under `cognis.tool.v2`. The text operation is the
default when `action` is absent. Rich actions use the serializable
`write_deliverable.rich` domain validator. Dashboard and Pulse also publish
their schema, example, rejection catalog, retry guidance, and valid skeleton.
`describe_tool`, `validate_tool_call`, write normalization, and persistence
therefore use the same contract. Unknown presentations and invalid registered
compositions are rejected before persistence.

For `write_deliverable`, the registered validator also receives the immutable
loaded-skill snapshot, successful tool evidence, materialized artifact
provenance, caller scope, and artifact store. Preflight therefore performs every
session-known deterministic check, including rich-media authorization and Daily
Brief source matching, without retaining or mutating artifacts. A successful
preflight returns a receipt binding the exact payload, schema hash, skill
contract/version, evidence, and caller scope. Execution always re-runs the same
pipeline. Changed payload, schema, skill, evidence, authorization, expiry, or
artifact bytes return `stale_validation`; persistence/storage failures after a
successful revalidation return `deliverable_runtime_failure`. Repeated identical
deterministic write failures followed by required `step_complete` attempts fail
the step after two write rejections instead of producing an unbounded reminder
loop.

The authoritative new-write contract is `cognis.rich.pulse.v2` and requires
`metadata.pulse_version=2`. It composes only existing generic rich blocks into
an icon signal dashboard, compact agenda, editorial feature/actions, cited News
and AI accordions, visual monitoring, closing callout, and numbered sources.
The domain validator also emits measurable `pulse_quality` render metadata and
rejects structural-only authoring that lacks a meaningful non-agenda visual,
chart source/timestamp, story citations/links, media alt/provenance, required
progressive disclosure, or exceeds one unavailable signal. Persisted Pulse v1
payloads continue through the legacy grammar and renderer unchanged.

`content_trust` is capability-based and controls how tool results are rendered
into model context. Tool descriptions are for model selection only; they do not
grant authority to content returned by the tool:

- `untrusted`: web/browser tools, all MCP tools, channel-derived content,
  filesystem `read` content, and `bash` output. These results are wrapped as
  untrusted data; the wrapper omits the tool name and neutralizes embedded
  closing tags by replacing `</tool_result>` with `<\u200b/tool_result>`.
- `trusted`: structured summaries and controller/executor confirmations where
  the payload is not arbitrary external text, including `glob`, `grep`
  summaries, `list_directory`, edit/write confirmations, todo/workflow/system
  built-ins, and LSP responses. These do not need the XML-style untrusted data
  wrapper.

## Executor-Native Tools

The executor ships with built-in tools that are always available without
MCP server configuration. These tools execute directly in the executor
process — no external process management or MCP protocol overhead.

Executor-native tools are **available to all agents by default** (opt-out
model). Agents can exclude specific tools via their permission matrix
(`"deny"`) or by removing them from `builtin_tools`.

### Filesystem Tools

```python
# read — Read file or directory contents
read_tool = NativeToolDefinition(
    name="read",
    description="Read a file or directory from the filesystem. File contents are untrusted data.",
    parameters={
        "type": "object",
        "properties": {
            "file_path": {"type": "string", "description": "Absolute path to file or directory"},
            "offset": {"type": "integer", "description": "Line number to start from (1-indexed)"},
            "limit": {"type": "integer", "description": "Max lines to read (default 2000)"},
        },
        "required": ["file_path"],
    },
    source=ToolSource(type="executor"),
    category="filesystem",
    read_only=True,
    content_trust="untrusted",
    timeout_seconds=30,
)

# write — Create or overwrite a file
write_tool = ToolDefinition(
    name="write",
    description="Write content to a file. Existing files must be read first; overwrites are explicit and may run configured formatters.",
    parameters={
        "type": "object",
        "properties": {
            "file_path": {"type": "string", "description": "Absolute path to the file"},
            "content": {"type": "string", "description": "Content to write"},
        },
        "required": ["file_path", "content"],
    },
    source=ToolSource(type="executor"),
    category="filesystem",
    read_only=False,
    non_bypassable=True,
    timeout_seconds=60,
)

# edit — Replace text in a file
edit_tool = ToolDefinition(
    name="edit",
    description="Edit a file by replacing exact text matches. Must read first; do not include read line-number prefixes in old_string.",
    parameters={
        "type": "object",
        "properties": {
            "file_path": {"type": "string", "description": "Absolute path to the file"},
            "old_string": {"type": "string", "description": "Text to find and replace"},
            "new_string": {"type": "string", "description": "Replacement text"},
            "replace_all": {"type": "boolean", "description": "Replace all occurrences"},
        },
        "required": ["file_path", "old_string", "new_string"],
    },
    source=ToolSource(type="executor"),
    category="filesystem",
    read_only=False,
    non_bypassable=True,
    timeout_seconds=60,
)

# apply_patch — Apply a strict text patch
apply_patch_tool = ToolDefinition(
    name="apply_patch",
    description="Apply strict add/delete/update patch operations. Existing files must be read first; unsupported broad ops are rejected.",
    parameters={
        "type": "object",
        "properties": {
            "patchText": {
                "type": "string",
                "description": "Patch text in apply_patch envelope syntax or the supported unified diff update subset",
            },
            "operation": {
                "type": "object",
                "description": "Native apply_patch operation with type, path, and optional diff",
            },
        },
        "anyOf": [{"required": ["patchText"]}, {"required": ["operation"]}],
    },
    source=ToolSource(type="executor"),
    category="filesystem",
    read_only=False,
    non_bypassable=True,
    timeout_seconds=60,
)

# multiedit — Multiple edits on one file
multiedit_tool = ToolDefinition(
    name="multiedit",
    description="Apply multiple sequential text replacements to a single file. Existing files must be read first.",
    parameters={
        "type": "object",
        "properties": {
            "file_path": {"type": "string", "description": "Absolute path to the file"},
            "edits": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "old_string": {"type": "string"},
                        "new_string": {"type": "string"},
                        "replace_all": {"type": "boolean"},
                    },
                    "required": ["old_string", "new_string"],
                },
            },
        },
        "required": ["file_path", "edits"],
    },
    source=ToolSource(type="executor"),
    category="filesystem",
    read_only=False,
    non_bypassable=True,
    timeout_seconds=60,
)

# list_directory — List directory contents
list_directory_tool = ToolDefinition(
    name="list_directory",
    description="List files and subdirectories in a directory.",
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Absolute path to the directory"},
            "ignore": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Glob patterns to ignore",
            },
        },
    },
    source=ToolSource(type="executor"),
    category="filesystem",
    read_only=True,
    timeout_seconds=30,
)
```

Filesystem mutation tools enforce freshness: modifying an existing file requires
a prior `read` in the same execution scope, and the recorded size/mtime stamp
must still match. A stale-stamp error tells the agent to re-read and notes that
a recent bash command or formatter may have changed the file.

Formatters are project-configured only (AD-7). Ruff runs for Python files only
when Ruff config is found up the tree (`[tool.ruff]` in `pyproject.toml`,
`ruff.toml`, or `.ruff.toml`). Prettier runs only when a Prettier config exists
(`package.json` `prettier` key or `.prettierrc*`). Formatter timeouts kill the
process and wait for cleanup. When the harness formatter changes a file, the
freshness stamp is re-recorded so an immediate second edit can proceed, and a
capped unified formatter diff is included in the model-visible tool output.

`edit` and `multiedit` require exact `old_string` matches. Failure diagnostics
include the nearest line-window snippet and targeted hints for common mistakes:
line-number prefixes copied from `read`, tab/space-only differences,
smart-quote/Unicode dash mismatches, and an unambiguous rstrip-normalized
fallback. Error wording uses `old_string`, matching the tool schema.

`read` reduces an over-budget line limit instead of middle-cutting the returned
content and tells the agent the effective limit to use for continuation reads.
Directory reads apply the default ignore list in addition to explicit ignores.
`.ipynb` files render as cell-structured text with outputs summarized; full
notebook editing remains a deferred dedicated tool.

### Search Tools

```python
# glob — Find files by pattern
glob_tool = ToolDefinition(
    name="glob",
    description="Find files matching a glob pattern and return absolute paths.",
    parameters={
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "Glob pattern (e.g. '**/*.py')"},
            "path": {"type": "string", "description": "Directory to search in"},
        },
        "required": ["pattern"],
    },
    source=ToolSource(type="executor"),
    category="search",
    read_only=True,
    timeout_seconds=30,
)

# grep — Search file contents
grep_tool = ToolDefinition(
    name="grep",
    description="Search file contents using regex patterns and return absolute paths.",
    parameters={
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "Regex pattern to search for"},
            "path": {"type": "string", "description": "Directory to search in"},
            "include": {
                "type": "string",
                "description": "File pattern filter; use brace syntax or comma-separated globs for multiple patterns (e.g. '*.py', '*.{ts,tsx}', '*.ts,*.svelte')",
            },
            "case_insensitive": {"type": "boolean", "description": "Case-insensitive search"},
            "context_lines": {"type": "integer", "description": "Context lines before and after matches"},
            "output_mode": {
                "type": "string",
                "enum": ["content", "files_with_matches", "count"],
                "description": "Return matching content, matching files, or counts",
            },
            "max_per_file": {"type": "integer", "description": "Maximum content-mode matches per file"},
        },
        "required": ["pattern"],
    },
    source=ToolSource(type="executor"),
    category="search",
    read_only=True,
    timeout_seconds=30,
)
```

`glob` returns absolute file paths. `grep` returns absolute paths in all modes,
supports `case_insensitive`, `context_lines`, `output_mode`, and
`max_per_file`, and threads those options to `rg` when available. Directory
searches cap content-mode output per file by default; single-file searches lift
that small cap unless `max_per_file` is supplied. Overflow messages are
actionable and suggest narrowing `path`/`include`, raising `max_per_file`, or
switching to `output_mode="files_with_matches"`/`"count"`.

### Shell Tools

```python
# bash — Execute shell commands
bash_tool = ToolDefinition(
    name="bash",
    description="Execute a shell command. Each call runs in a fresh shell: cd/export do not persist; use workdir/env.",
    parameters={
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "Shell command to execute"},
            "description": {"type": "string", "description": "Brief description of what this does"},
            "timeout": {"type": "integer", "description": "Timeout in milliseconds"},
            "workdir": {"type": "string", "description": "Working directory"},
            "env": {"type": "object", "description": "Per-call environment variables"},
            "run_in_background": {"type": "boolean", "description": "Return a shell_id for managed polling"},
            "target_executor": {"type": "string", "description": "Optional assigned executor ID for this call"},
        },
        "required": ["command"],
    },
    source=ToolSource(type="executor"),
    category="shell",
    read_only=False,
    non_bypassable=True,
    content_trust="untrusted",
    timeout_seconds=3605,
)

# bash_output — Poll background shell output
bash_output_tool = ToolDefinition(
    name="bash_output",
    description="Read new output from a background bash session.",
    parameters={
        "type": "object",
        "properties": {
            "shell_id": {"type": "string"},
            "cursor": {"type": "integer"},
            "target_executor": {"type": "string"},
            "filter_regex": {"type": "string", "description": "Case-insensitive line filter"},
        },
        "required": ["shell_id"],
    },
    source=ToolSource(type="executor"),
    category="shell",
    read_only=True,
    content_trust="untrusted",
    timeout_seconds=30,
)
```

Foreground `bash` commands default to a 120,000 ms timeout and may request up to
3,600,000 ms. Foreground output is bounded with a ring buffer (head 100K chars
and tail 300K chars). When a foreground command times out or the tool call is
cancelled, the executor sends SIGTERM to the process group where supported,
waits 2 seconds, then SIGKILLs any remaining process tree before returning or
propagating cancellation; Windows cleanup is limited to the shell process unless
the platform provides stronger process-tree support. Timeout errors warn that a
write may already have succeeded before cleanup completed.

Use `run_in_background=true` for long-running builds, deployments, and watchers.
Background commands return a managed `shell_id`; use `bash_output` to poll output
and `bash_kill` to stop them. For background commands, `timeout` only controls
the initial preview wait and does not limit process lifetime. Prefer these
managed controls over process-name polling such as `pgrep`, which can match
wrappers or the polling command itself.

A user can move a running foreground `bash` call into the background from the
tool card ("Move to background"). The controller resolves the call's executor
and executor instance from the active turn's durable dispatch record and sends a
`tool.background` RPC (forwarded to the owning replica when needed). The bash
handler stops its foreground stream readers without consuming buffered data,
adopts the still-running process as a normal background shell session (seeded
with the output captured so far, same conversation, call id, and completion
callback), and returns the usual background-started result marked
`moved_to_background: true` / `background_initiator: "user"`. The foreground
timeout no longer applies. If the command exits first, the normal foreground
result wins. For an executor-native backgroundable tool, the agent loop then
injects a one-off system notice before the next model call telling the agent the
user moved the command (with its `shell_id`) and persists it as a
`system_notice` lifecycle event (`kind: tool_moved_to_background`). Completion
follow-ups and status reminders behave exactly as for `run_in_background=true`.

Agents should provide the optional `description` argument for background bash
commands. The description is treated as the human-readable job identifier in
prompt reminders and completion follow-ups, so it should say what the command is
for rather than restating the raw command.

While background bash jobs are running, the agent loop injects a concise
`<background_shell_status>` reminder before model calls. The reminder includes
the shell id, executor id/type, PID when available, description, command summary,
runtime, idle time, buffered-output size, and output cursor. It shows the three
most recent running jobs in detail; additional jobs are summarized by count plus
shell ids and PIDs. If jobs exist on multiple assigned executors, each job keeps
its executor identity so the agent can route `bash_output` or `bash_kill` to the
matching executor with `target_executor` when available. The lifecycle is
append-only and cache-safe: the full reminder text is appended only when material
content changes, prefixed with `(supersedes earlier reminder)` after the first
version. Volatile `running_for` / `idle_for` values are ignored for change
detection, and the loop never mutates or pops prior reminder messages. TODO
reminders are seeded once per turn; tool-result echoes carry fresh TODO state.

When a background bash command exits normally or with a non-zero status, the
executor sends a `shell.background_completed` notification to the controller.
The controller converts this to the same-conversation follow-up path used by
other asynchronous work, so the agent receives a system-initiated turn with the
shell id, executor, exit code, description, command summary, runtime, and recent
output tail. Explicit `bash_kill` and executor cleanup suppress completion
follow-ups to avoid noisy notifications for intentionally stopped or abandoned
jobs.

### Multi-Executor Targeting

Executor-routed tools may include an optional `target_executor` parameter. This
parameter is consumed by the controller and stripped before the tool request is
sent over JSON-RPC to the executor.

Rules:

- `target_executor` must be a real executor ID resolved from the agent's primary
  or additional executor bindings. Aliases are not supported.
- If `target_executor` is omitted, the call routes to the current active
  executor.
- If `target_executor` is present, only that call routes to the named executor;
  the active executor does not change.
- If the target executor is unavailable, the tool is not evaluated by Intaris
  and no RPC is sent. The controller returns a factual error, for example:
  `Target executor "infra-runner" is offline; tool was not executed.`
- If the target executor is available but does not expose the requested tool,
  the controller returns a tool error listing the executor state and available
  assigned executors for that tool.
- Guardrails evaluation includes the resolved target executor ID, primary vs.
  additional membership, labels, runtime state, and environment snapshot when
  available.

When an executor-routed tool is exposed to the model, the controller augments
the JSON schema with `target_executor` for tools that can run on more than one
assigned executor. The underlying `ToolDefinition` remains executor-neutral so
the same definition can be reused across in-process, subprocess, websocket, and
compiled-lite executors.

### Web Tools

Web tools support configurable backends. The default backend is set via the
`web.backend` setting (`"direct"`, `"tavily"`, or `"brave"`). Each tool call
can override the default with a `backend` parameter.

**Backends:**

| Backend | Fetch | Search | Crawl/Map/Research | API Key Required |
|---------|-------|--------|--------------------|------------------|
| `direct` | httpx + browser headers | DuckDuckGo | no | no |
| `tavily` | Tavily Extract | Tavily Search | yes | yes (`tavily_api_key`) |
| `brave` | falls back to direct | Brave Web Search | no | yes (`brave_api_key`) |

```python
# web_fetch — Fetch URL content (supports direct + tavily backends)
# web_search — Search the web (supports direct + tavily + brave backends)
# web_crawl — Crawl a website (requires tavily)
# web_map — Map site structure (requires tavily)
# web_research — Deep research (requires tavily)
```

The direct backend uses browser-like headers (Chrome User-Agent, Accept,
Sec-Fetch-* headers) to avoid bot detection. It retries on HTTP 429 with
exponential backoff and Retry-After header support. Cloudflare-protected
sites that require browser access return an actionable error suggesting
the Tavily backend.

### Safety Classification

| Tool | read_only | non_bypassable | Rationale |
|------|-----------|----------------|-----------|
| `read` | yes | no | Read-only filesystem access |
| `write` | no | **yes** | Creates/overwrites files |
| `edit` | no | **yes** | Modifies file content |
| `apply_patch` | no | **yes** | Modifies files via diff |
| `multiedit` | no | **yes** | Modifies file content |
| `list_directory` | yes | no | Read-only directory listing |
| `glob` | yes | no | Read-only file search |
| `grep` | yes | no | Read-only content search |
| `bash` | no | **yes** | Arbitrary command execution |
| `web_fetch` | yes | no | Read-only URL fetching |
| `web_search` | yes | no | Read-only web search |
| `web_crawl` | yes | no | Read-only website crawling |
| `web_map` | yes | no | Read-only site structure mapping |
| `web_research` | yes | no | Read-only research |

Write operations always go through Intaris evaluation regardless of agent
permissions. Read-only tools can be auto-approved by agent permission matrix.

Filesystem edit tools may append executor-local LSP diagnostics after a file is
changed. This is best-effort feedback from the selected executor runtime: the
edit still succeeds if LSP is disabled, unavailable, or times out during first
server startup. `read` may warm LSP in the background, but it does not block on
diagnostics. Edit results carry bounded `lsp_diagnostics` metadata (status,
injection outcome, counts, bytes) that the controller counts into
`cognis_lsp_edit_*` metrics at the tool routing boundary.

The executor-local `lsp` tool offers deliberate semantic navigation:
`goToDefinition`, `typeDefinition`, `goToImplementation`, `findReferences`,
`hover`, `documentSymbol`, `workspaceSymbol`, `incomingCalls`,
`outgoingCalls`, `outline`, `capabilities`, and `diagnostics`. Operations are
gated on capabilities negotiated with each running server, output is compact
text with workspace-relative paths and 1-based coordinates, and results are
bounded by `limit` and a byte budget with an explicit `truncated` flag. Result
metadata records `operation`, `status`, `result_count`, `output_bytes`,
`truncated`, and per-server outcomes; the controller counts these into
`cognis_lsp_tool_*` metrics. See `docs/guide/executors.md` for the operation
table.

The `apply_patch` tool accepts strict text-only patches in two forms:
- full apply_patch envelope syntax for `Add File`, `Update File`, `Delete File`, and `Move to`
- the supported unified diff subset for updates to existing files
- native OpenAI Responses operation objects (`create_file`, `update_file`, `delete_file`)

Existing-file `apply_patch` updates, deletes, and moves require a prior `read` of the
source file in the same execution scope. `Add File` does not. Unsupported apply_patch
forms fail deterministically instead of falling back to fuzzy matching. EOF
markers are supported: `*** End of File` is accepted inside apply_patch envelopes,
and `\ No newline at end of file` preserves final-newline semantics in apply_patch
envelopes and unified-diff hunks.

## Built-in Orchestration Tools

These are handled by the controller directly — they are not tool executions
but session management operations.

```python
# delegate — Run a bounded secondary/specialist sub-session
delegate_tool = ToolDefinition(
    name="delegate",
    description="Delegate a task to a specialized agent.",
    parameters={
        "type": "object",
        "properties": {
            "agent_id": {"type": "string", "description": "Eligible secondary agent ID"},
            "task": {"type": "string", "description": "Task description"},
            "context": {"type": "string", "description": "Background context"},
            "expected_output": {"type": "string"},
        },
        "required": ["task", "agent_id"],
    },
    source=ToolSource(type="builtin"),
)
```

`delegate` targets only active, visible, accessible secondary agents. Effective
enabled system secondaries are available to primary agents; user-defined
secondaries must also be explicitly bound to the caller. Primary agents,
including the caller itself, use managed conversations instead. Existing
historic primary delegate rows remain readable, but retry, follow-up, and fork
cannot create new primary delegate runs.

Managed conversations target only active, visible, accessible primary
non-system agents. Top-level self-targeting remains supported. Both target
domains are derived from effective `AgentRegistry` metadata; there is no
keyword routing or duplicated role map.

The controller captures one caller-scoped discovery snapshot per turn and uses
it to enrich the model-visible JSON Schema, authoritative tool descriptor,
`describe_tool`, `validate_tool_call`, and controller argument validation.
Dynamic option records include canonical agent IDs, names, descriptions, and
eligible runtime profiles. The snapshot is discovery only: target eligibility
is refreshed immediately before any new delegate or managed-conversation turn
is created.

When a delegated child session completes, Cognis stores a durable bounded
`result_content` value and returns it once in the `delegate` tool payload as
`result` for immediate synthesis. Cognis prefers explicit workflow deliverable
content when present; otherwise it aggregates all child `assistant_message`
contents in chronological order with `[[message:n]]` anchors and
`--- Assistant message n ---` separators. Tool-result metadata exposes
`output_anchors`, including per-message anchors, so agents can use
`list_tool_output_anchors` and `read_tool_output_anchor` to recover one assistant
message instead of reloading the full delegate output. Markdown ATX headings in
saved tool outputs and delegate result content are also exposed as supplemental
`heading:<slug>` anchors without injecting marker lines into the Markdown.
Anchors are derived from the final bounded result content, so truncated-away
messages or headings are not advertised as readable sections. `list_subsessions`
remains compact, while `get_subsession` returns the durable result content and
bounded per-message/heading sections after completion.

These tools submit **requests** to the Decision Engine, which approves,
modifies, or rejects them. The LLM cannot force delegation.

### Built-in Executor Routing Tools

Executor routing tools are controller-handled. They mutate turn-local runtime
state and never execute on an executor.

```python
# switch_executor — Change the active executor for subsequent executor-routed tools
switch_executor_tool = ToolDefinition(
    name="switch_executor",
    description=(
        "Switch the active executor for subsequent executor-routed tool calls. "
        "Use target_executor on individual tools for one-off execution instead."
    ),
    parameters={
        "type": "object",
        "properties": {
            "target_executor": {"type": "string", "description": "Assigned executor ID"},
            "reason": {"type": "string", "description": "Why this executor is needed"},
        },
        "required": ["target_executor"],
    },
    source=ToolSource(type="builtin"),
    category="orchestration",
    read_only=True,
)
```

`switch_executor` validation:

- The target must be in the resolved primary or additional executor set for the
  current agent.
- The target must be usable: active DB status, `active` or `degraded` runtime
  state, matching desired/applied config generation, ready connection for
  remote executors, and at least one observed tool.
- On success, the tool result includes the target executor's environment and
  available tool summary.
- On failure, the active executor is unchanged and the result states the target
  executor's factual state. The result must not speculate about why the
  executor is unavailable.
- If the agent is active on a non-primary executor, Cognis injects a reminder to
  switch back to a primary executor when the non-primary work is complete.
- If the active non-primary executor becomes unavailable and the work requires
  that executor, Cognis notifies the user and cancels the turn instead of
  continuing on a different host.

### Built-in Agent Profile Switching

`switch_agent_profile` is a controller-handled runtime-routing tool. It is
exposed only when the current agent has at least one enabled runtime profile
with `agent_switchable=true`. Eligible profile IDs and their descriptions are
listed in the mutable runtime-profile system block after the immutable cache
breakpoint; the tool schema remains stable and does not contain a dynamic enum.

The tool requires `profile_id` and a non-empty, bounded `reason`. The reason is
a concise operational routing justification, not private chain-of-thought.
`switch_agent_profile` must be the only tool call in its assistant tool batch,
and only one successful switch is allowed per logical turn.

On success Cognis:

1. persists the profile to the active session and, for root conversations, the
   conversation;
2. clears `/model` and `/thinking` overrides;
3. records the matching tool result and a visible system notice containing the
   reason;
4. injects an authoritative profile-change system message for the model; and
5. reassembles context and continues the same `turn_id` under the new profile.

Delegated child sessions persist switches to the child session only. A
reasoning-effort-only switch with the same provider/model preserves the
byte-stable immutable prefix and stable tool schema, allowing provider prompt
cache reuse.

## Built-in Task Inspection Tools

These are controller-handled read/write tools for the durable task queue.
They let the main chat agent inspect and manage background work when the
user asks things like "what finished recently?" or "show me blocked tasks".

```python
# list_tasks — Query tasks visible to the current user/agent
list_tasks_tool = ToolDefinition(
    name="list_tasks",
    description="List tasks by status, agent, workflow, or recent completion.",
    parameters={
        "type": "object",
        "properties": {
            "status": {"type": "string"},
            "agent_id": {"type": "string"},
            "workflow_id": {"type": "string"},
            "recently_finished": {"type": "boolean"},
            "limit": {"type": "integer", "default": 20},
        },
    },
    source=ToolSource(type="builtin"),
    read_only=True,
)

# get_task_status — Detailed status/progress for one task
get_task_status_tool = ToolDefinition(
    name="get_task_status",
    description="Get current status, workflow step, gates, and result summary for a task.",
    parameters={
        "type": "object",
        "properties": {
            "task_id": {"type": "string"},
        },
        "required": ["task_id"],
    },
    source=ToolSource(type="builtin"),
    read_only=True,
)

# update_task — Pause/resume/cancel or reprioritize a task
update_task_tool = ToolDefinition(
    name="update_task",
    description="Pause, resume, cancel, or reprioritize a task.",
    parameters={
        "type": "object",
        "properties": {
            "task_id": {"type": "string"},
            "action": {"type": "string", "enum": ["pause", "resume", "cancel"]},
            "priority": {"type": "integer"},
        },
        "required": ["task_id"],
    },
    source=ToolSource(type="builtin"),
)
```

These tools are useful in the main chat because completed/paused/failed task
events may arrive while the conversation is active or idle. The chat agent can
query the queue and answer naturally, instead of relying only on pushed events.

## MCP Integration

### Global MCP Servers (DB-Managed)

MCP servers are configured globally in the ``mcp_servers`` DB table and
assigned to executors via ``config.mcp_server_ids``.  Agents inherit MCP
tools from their assigned executor.

```sql
CREATE TABLE mcp_servers (
    server_id         TEXT PRIMARY KEY,
    name              TEXT NOT NULL,
    transport         TEXT NOT NULL DEFAULT 'stdio',
    command           TEXT,              -- Required for stdio
    url               TEXT,              -- Required for sse/streamable_http
    args              JSON,
    env               JSON,              -- stdio only: {"KEY": "value"} or {"KEY": "$secret:name"}
    headers           JSON,              -- http only: {"Authorization": "value"} or "$secret:name"
    auth_config       JSON,              -- {"type":"none"|"static_headers"|"oauth2", ...}
    timeout_seconds   INTEGER NOT NULL DEFAULT 30,
    description       TEXT,
    owner_email       TEXT REFERENCES users(email),
    status            TEXT NOT NULL DEFAULT 'active',
    UNIQUE(name, owner_email)
);
```

MCP servers are **user-scoped** — each user manages their own servers.
Assignment to executors is validated: ``config.mcp_server_ids`` may only
reference servers owned by the same user as the executor.

### Local MCP (Executor-Hosted)

Local MCP servers run on the executor.  During ``executor.configure``, the
controller resolves assigned MCP servers from the DB, resolves secrets, and
sends the configs to the executor. The executor starts transport-specific MCP
clients (``stdio``, ``sse``, or ``streamable_http``), discovers tools, and
includes them in ``tool.list``.

Flow: Controller evaluates via Intaris → approved → tool.execute to Executor →
Executor calls local MCP server → result back to Controller.

**Supported transports for executor-hosted MCP:** ``stdio``, ``sse``, and
``streamable_http`` across websocket, subprocess, and in-process executor
modes. Transport-specific configuration is strict: ``stdio`` uses ``env`` and
HTTP transports use ``headers``.

HTTP MCP servers support three authentication modes:

- ``none``: no controller-managed authentication.
- ``static_headers``: legacy/static HTTP headers. Static ``Authorization`` is
  still accepted in this mode for backwards compatibility.
- ``oauth2``: OAuth 2.1 for HTTP MCP servers. Cognis supports
  authorization-code + PKCE and device-code flow; ``auth_config.flow`` may be
  ``auto`` (default), ``authorization_code``, or ``device_code``. This mode is
  valid only for ``sse`` and ``streamable_http`` transports. Static
  ``Authorization`` headers are rejected when OAuth is enabled; non-auth headers
  remain allowed.

MCP OAuth tokens are encrypted and scoped by
``user_email + mcp_server_id + issuer + resource_key`` where a missing resource
uses a deterministic empty resource key. The controller stores refresh/access
tokens and OAuth transaction state; executors receive only an injected
``Authorization: Bearer <access_token>`` header during configuration. The
controller proactively refreshes active tokens before expiry, persists bounded
retry/error state, and advances assigned executor generations so long-lived MCP
clients receive the committed replacement header. Refresh calls use the bounded
``COGNIS_MCP_OAUTH_REFRESH_TIMEOUT_SECONDS`` wall-clock timeout. A timeout or
transport failure after request transmission is treated as an indeterminate
rotation outcome and requires reauthorization rather than retrying a possibly
consumed refresh token. Runtime 401/``invalid_token`` failures trigger one
single-flight refresh, reconfigure, and tool retry; a second authorization
failure invalidates the refresh chain and removes the unusable server from the
converged tool manifest. Refresh single-flight coordination is process-local;
deployments must currently run one controller process for rotating refresh
tokens. OAuth
authorization challenges reuse ``auth_challenge`` notifications with
``kind="oauth_authorization"`` and carry the available runtime routing metadata
(``conversation_id``, ``session_id``, ``task_id``, and step identifiers when the
caller supplies them). Authorization-code challenges complete through the public
callback. Device-code challenges expose only the provider verification URI and
user code, poll on the controller until the provider returns tokens, then resolve
the notification internally; channel replies must not complete either flow.
``delivery_mode="silent"`` persists the challenge without outward notification;
an interaction override of ``none`` should report structured
``authorization_required`` instead of pausing.
First-use challenges created from executor configuration paths may not have a
user-visible conversation yet; they are still persisted with internal routing
metadata and suppressed from outward delivery until a routed challenge can be
shown.

Discovery follows protected-resource metadata / ``WWW-Authenticate`` hints,
authorization-server metadata with OIDC fallback, issuer consistency checks,
PKCE S256 for authorization-code flow, advertised
``device_authorization_endpoint`` support for device-code flow, and the OAuth
resource parameter. In ``auto`` mode, existing configured ``client_id`` or
``redirect_uri`` keeps the authorization-code path; otherwise Cognis may use
device-code flow when the authorization server advertises it. User-provided
``authorization_params`` may add provider-specific non-reserved parameters but
cannot override controller-owned OAuth fields such as ``state``,
``redirect_uri``, ``client_id``, ``response_type``, ``code_challenge``, or
``code_challenge_method``. Metadata/token endpoints are bounded by
HTTPS/localhost rules, manually validated redirect limits, DNS-based private
address rejection, short timeouts, and token redaction. Dynamic client
registration failures include sanitized HTTP status and provider
``error``/``error_description`` details in logs and operator-facing failures,
without logging tokens, client secrets, credential headers, or raw secret
payloads. Callback failures after state consumption mark the transaction and
linked notification failed instead of leaving a pending challenge. Runtime MCP
401/403 failures are represented as structured MCP auth errors so one
unavailable OAuth MCP server does not crash the executor. Automatic retry after
callback is conservative:
configuration/list retries can be retried when still resumable; already-failed
tool calls should be retried by the agent after the callback unless their
original call is still safely owned by the current waiter.

Note: Common developer tools (filesystem, shell, search) are executor-native
and do not require MCP server configuration.  Local MCP is for specialized
servers (databases, custom APIs, etc.).

### Intaris-Managed MCP (Remote)

Remote MCP servers registered in Intaris.  Intaris acts as MCP proxy,
evaluating safety AND executing in one call.

```yaml
# In agent definition (agent-level, not executor-dependent)
tools:
  intaris_mcp_servers: ["github", "slack"]
```

Available Intaris MCP servers are auto-discovered via ``GET /api/v1/mcp/servers``
on Intaris.  The agent form shows a multi-select dropdown of discovered servers.
Server configuration (credentials, endpoints) is managed on the Intaris side.

Flow: Controller calls Intaris ``POST /api/v1/mcp/call`` → Intaris evaluates +
proxies to remote MCP server → result back to Controller.  Executor not
involved.

Intaris MCP tools are resolved at session setup and injected into the tool
registry alongside executor-provided tools.  They are included in the
effective-tools API response and the tool exposure layer.

### Tool Discovery and Inventory Assembly

At session setup, the controller assembles the **full effective inventory**
from all sources.  This is the internal source of truth — the LLM does NOT
see all of these directly.

```python
full_effective_inventory = (
    executor_native_tools           # Always available (opt-out per agent)
    + builtin_orchestration_tools   # delegate + task/workflow tools
    + builtin_system_tools          # list_agents, get_status, search_tools
    + builtin_memory_tools          # memory_search, memory_add, etc.
    + builtin_workflow_tools        # step_complete, step_todo_write, etc.
    + web_tools                     # web_fetch, web_search (from executor)
    + local_mcp_tools               # From executor's assigned MCP servers
    + intaris_mcp_tools             # From Intaris /mcp/tools (agent-assigned)
    + skill_tools                   # From active skills
)
```

The **tool exposure layer** then derives the model-facing tool set:

```python
model_facing_tools = tool_exposure.prepare(
    full_inventory=full_effective_inventory,
    provider=resolved_provider,
    model=resolved_model,
)
# Returns: core tools (always loaded) + deferred tools (provider-specific)
```

The LLM sees a model-facing tool set derived from the step profile and provider
capabilities. It can discover additional eligible tools via provider-native
tool search, the generic ``search_tools`` builtin, or ``skill_load`` when a
skill activates deferred tool ids. See the "Tool Exposure Architecture"
section for details.

MCP descriptions are upstream-controlled, model-facing text. Local and Intaris
MCP definition builders clamp descriptions to 1024 characters and append
`full description via search_tools` when truncated. JSON Schema metadata keys
(`$schema`, `$id`, `$comment`) are stripped recursively before provider
exposure; this guard applies both at MCP definition ingestion and at final tool
schema emission.

## Tool Permission Evaluation

Shared tool semantics are runtime-neutral:

- `allow`
- `deny`
- `evaluate`
- `non_bypassable`
- timeout
- cancel

The `native` runtime enforces these through the existing controller-driven tool
loop. External runtimes must translate the same semantics to native runtime
enforcement without changing user-facing policy behavior.

```python
async def evaluate_tool_call(self, tool_call, agent, session):
    """
    Permission check flow:
    1. Non-bypassable check (system-level, always goes through guardrails)
    2. Agent permission matrix
    3. Guardrails evaluation (Intaris)
    """
    tool = self.registry.get(tool_call.name)

    # Step 1: Non-bypassable tools ALWAYS go through guardrails
    if tool and tool.non_bypassable:
        return await self._evaluate_via_guardrails(tool_call, session)

    # Step 2: Agent permission matrix
    permission = agent.permissions.resolve_permission(tool_call.name)
    if permission == Permission.DENY:
        return PermissionDecision(decision="deny", source="agent_permission")
    if permission == Permission.ALLOW:
        return PermissionDecision(decision="approve", source="agent_permission")

    # Step 3: EVALUATE → Intaris
    return await self._evaluate_via_guardrails(tool_call, session)
```

### Non-Bypassable Tools

Certain tool categories always go through guardrails regardless of agent
permissions. Configured system-wide:

Configured in the `settings` DB table (key: `security.non_bypassable_tools`),
managed via Settings UI or `PUT /api/v1/settings/security.non_bypassable_tools`:

```json
["bash", "write", "edit", "apply_patch", "multiedit", "*/create_*", "*/delete_*"]
```

This prevents an agent with `"*": "allow"` from bypassing safety checks on
destructive operations.

### MCP Tool Naming

MCP tool names must be safe for all LLM providers.  OpenAI requires tool
names to match ``^[a-zA-Z0-9_-]+$`` — no slashes, dots, or spaces.

**Model-visible name format:**

| Source | Internal routing | Model-visible name |
|--------|------------------|--------------------|
| Executor-native | ``ToolSource(type="executor")`` | ``read``, ``bash``, ``glob`` |
| Local MCP | ``ToolSource(type="local_mcp", server_name="postgres", raw_tool_name="query")`` | ``mcp_postgres__query`` |
| Intaris MCP | ``ToolSource(type="intaris_mcp", server_name="github", raw_tool_name="create_issue")`` | ``mcp_github__create_issue`` |

**Sanitization rules:**

- Replace ``/`` with ``__`` (double underscore)
- Replace any character not in ``[a-zA-Z0-9_-]`` with ``_``
- Prefix MCP tools with ``mcp_``
- If sanitization would create a collision, append a deterministic short suffix
  derived from the stable MCP identity so the visible tool names stay unique
- Store original server name and tool name in ``ToolSource.server_name``
  and ``ToolSource.raw_tool_name`` for dispatch

**Dispatch uses source metadata, not name parsing:**

```python
# Old (broken for OpenAI):
_, raw_tool_name = tool_call.name.split("/", 1)

# New (provider-safe):
server_name = registered_tool.definition.source.server_name
raw_tool_name = registered_tool.definition.source.raw_tool_name
```

**Stable tool IDs** for permission matching and UI:

- Builtin/native: ``builtin:<tool_name>``
- MCP: ``mcp:<server_id>:<raw_tool_name>``

Permission matching supports both stable IDs and plain names:

```yaml
permissions:
  tool_permissions:
    "*": "evaluate"                              # Default
    "read": "allow"                              # Executor-native read
    "builtin:glob": "allow"                      # Stable ID form
    "mcp_postgres__query": "evaluate"            # Model-visible name
    "mcp:mcp_postgres:query": "evaluate"         # Stable ID form
```

## Tool Router

```python
class ToolRouter:
    ORCHESTRATION_TOOLS = {"delegate", ...task/workflow tools...}
    CONTROLLER_TASK_TOOLS = {"list_tasks", "get_task_status", "update_task"}

    async def route(self, tool_call, session, agent, executor):
        # Orchestration → controller handles directly
        if tool_call.name in self.ORCHESTRATION_TOOLS:
            return await self.decision_engine.handle_orchestration(
                session, tool_call
            )

        # Task queue inspection/control → controller handles directly
        if tool_call.name in self.CONTROLLER_TASK_TOOLS:
            return await self.task_service.handle_tool_call(session, tool_call)

        # Intaris-managed MCP → Intaris proxy
        tool = self.registry.get(tool_call.name)
        if tool and tool.source.type == "intaris_mcp":
            return await self.guardrails.call_mcp_tool(
                session_id=session.intaris_session_id,
                server_name=tool.source.server_name,
                tool_name=tool.source.raw_tool_name,  # Use metadata, not name parsing
                arguments=tool_call.arguments,
            )

        # Executor-native and local MCP → resolve target, evaluate, execute
        target = await executor_pool.resolve_tool_target(tool_call, agent)
        if not target.usable:
            return ToolResult(
                output=f'Target executor "{target.executor_id}" is {target.runtime_state}; tool was not executed.',
                is_error=True,
            )

        decision = await self.evaluate_tool_call(
            tool_call.without_controller_args(), agent, session, target_executor=target
        )
        if decision.decision == "approve":
            return await target.connection.tool_execute(tool_call.without_controller_args())
        elif decision.decision == "deny":
            return ToolResult(denied=True, reasoning=decision.reasoning)
        elif decision.decision == "escalate":
            user_decision = await self.wait_for_escalation(
                session, tool_call, decision
            )
            if user_decision == "approve":
                return await executor.tool_execute(tool_call)
            return ToolResult(denied=True, reasoning="User denied")
```

## Tool Exposure Architecture

### Problem: Large Tool Inventories

Agents may have access to many tools from multiple sources (executor-native,
MCP, Intaris MCP, skills).  Sending all tool schemas to the LLM on every
request creates three problems:

1. **Provider limits** — OpenAI enforces a hard cap of 128 tools per request.
2. **Token cost** — 50+ tools can consume 10K-55K+ tokens before the
   conversation even starts.  Anthropic has measured 134K tokens for tool
   definitions alone in real deployments.
3. **Accuracy degradation** — models make worse tool selections with large,
   similar-looking tool sets.  Anthropic's testing showed accuracy improving
   from 49% to 74% (Opus 4) when using deferred loading instead of
   loading all tools upfront.

### Solution: Two-Tier Tool Exposure

The full effective tool inventory is the internal source of truth.  A
separate **tool exposure layer** derives the model-facing tool set before
each LLM call.

```
┌─────────────────────────────────────────────────────┐
│  Full Effective Inventory (internal)                  │
│  - All tools from all sources                        │
│  - Stable internal IDs                               │
│  - Used for: effective-tools API, agent editor,      │
│    permission resolution, runtime dispatch            │
│  - NOT sent to the model directly                    │
└──────────────────────┬──────────────────────────────┘
                       │
          ┌────────────┴────────────┐
          │                         │
┌─────────▼──────────┐  ┌──────────▼──────────────────┐
│  Core Tools         │  │  Deferred / Discoverable    │
│  (always loaded)    │  │  Tools                      │
│                     │  │                             │
│  - memory           │  │  - Executor MCP tools       │
│  - orchestration    │  │  - Intaris MCP tools        │
│  - step/task/wf     │  │  - Overflow tools           │
│  - filesystem       │  │  - Low-frequency tools      │
│  - shell            │  │                             │
│  - web              │  │  Loaded on demand via       │
│  - system/image     │  │  provider-specific mechanism │
│                     │  │  or generic tool search      │
│  Stable across      │  │                             │
│  turns → cached     │  │  Does NOT break cache       │
└─────────────────────┘  └─────────────────────────────┘
```

### Provider-Specific Mechanisms

Each LLM provider has a different optimal approach for handling deferred
tools while preserving prompt caching:

#### OpenAI

**For gpt-5.4+ models (Responses API):**

- Use the OpenAI Responses transport, but keep Cognis' internal transcript
  canonical.  A provider-boundary bridge translates canonical chat/tool
  messages into Responses ``input`` and normalizes Responses output back into
  the existing assistant/tool-call delta shape used by the agent loop.
- Responses-capable OpenAI models use a **stable full inventory** instead of
  falling back to generic slot-based trimming.  This keeps same-turn tool
  visibility aligned with the effective runtime.
- Tool exposure invariants remain unchanged:
  - stable ``stable_tool_id()`` values
  - deterministic ordering by category/source/stable ID
  - alias preservation from visible names back to internal names
  - cache-stable schema ordering/bytes across turns
- ``search_tools`` remains the fallback for non-Responses or unsupported
  models/providers.

**For older models (Chat Completions API):**

- Hard limit of **128 tools** per request.
- Use ``allowed_tools`` in ``tool_choice`` to restrict which tools the model
  can call **without changing the ``tools`` array**:

  ```python
  tool_choice = {
      "type": "allowed_tools",
      "mode": "auto",
      "tools": [
          {"type": "function", "name": "read"},
          {"type": "function", "name": "bash"},
          # ... only core tools listed here
      ]
  }
  ```

  The full toolkit stays in the cached prefix; per-turn restrictions live
  in request metadata only.  Cache is preserved.

- If total tools exceed 128, use a generic controller-side tool search
  fallback (see below).

#### Anthropic Claude

- Use ``defer_loading: true`` on MCP/overflow tools.
- Include a **Tool Search Tool** (regex or BM25 variant) in the tools array.
- Deferred tools are excluded from the system-prompt prefix entirely.
- When Claude discovers a deferred tool through search, the definition is
  appended inline as a ``tool_reference`` block in conversation history.
- **The prefix is untouched, so prompt caching is preserved.**
- Anthropic reports 85% token reduction and significant accuracy improvements.
- LiteLLM supports ``defer_loading`` for Anthropic via the
  ``tool-search-tool-2025-10-19`` beta header.

**Cache breakpoints for tools and history:**

Place ``cache_control: {"type": "ephemeral", "ttl": ...}`` on the last
provider-facing tool schema for every Anthropic-compatible strategy, not only
the deferred-loading path. This caches the entire tool-definitions prefix.
Anthropic supports up to 4 cache breakpoints. Cognis recomputes breakpoint
indices per model cycle, not just once per turn, and uses all four in prompt
order when present:

1. Last tool definition (caches the entire tool schema prefix for all Anthropic
   strategies, not only deferred-loading paths)
2. Last cached system/project-context prefix message (immutable identity/runtime/
   memory/skills/continuation summary plus any frozen project context)
3. End of prior-turn history (moving per turn)
4. Last message of the current request/tool cycle (moving per cycle)

Cache hierarchy remains ``tools → system → messages``. A change at one level
invalidates that level and everything after it. Follow-up guidance and volatile
executor state are mutable suffix reminders, so follow-up turns do not rewrite
the immutable prefix. ``session.anthropic_cache_ttl`` defaults to ``"5m"``.
When set to ``"1h"``, Cognis uses 1h TTL only for the tool-schema and cached
prefix/project-context breakpoints, keeps moving history/current-cycle
breakpoints at 5m, and sends the ``extended-cache-ttl-2025-04-11`` beta header
on both supported Anthropic transports.

#### Google Gemini

- Supports **context caching** (explicit and implicit).
- Minimum cacheable content: 1024-4096 tokens depending on model.
- No equivalent of ``defer_loading`` or ``tool_search``.
- Use the generic controller-side tool search fallback for large inventories.

#### Other Providers

- Use the generic controller-side tool search fallback.
- Keep core tools in the ``tools`` array.
- Add a ``search_tools`` builtin tool for discovering overflow tools.

### Generic Tool Search Fallback

For providers without native tool search (or as a universal fallback), Cognis
provides a controller-side ``search_tools`` builtin tool:

```python
search_tools = ToolDefinition(
    name="search_tools",
    description=(
        "Search for additional tools available in this session. "
        "Use when you need a capability not in your current tool set. "
        "Returns matching tool names and descriptions."
    ),
    parameters={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Natural language search query"
            },
            "category": {
                "type": "string",
                "description": "Filter by tool category (e.g. 'mcp', 'filesystem')"
            },
        },
        "required": ["query"],
    },
    source=ToolSource(type="builtin"),
    category="system",
    read_only=True,
)
```

The handler searches the current step's hidden eligible inventory (including
deferred tools that survived profile filtering but excluding tools already
visible to the model) and returns matching tool definitions. The agent loop
then injects discovered tools into the next turn's ``tools`` array.

``search_tools`` is a controller-managed, read-only system builtin.  It does
not execute arbitrary tools itself; it only reveals the already effective,
permission-filtered inventory for the current step.

### Discovery Strategy Matrix

Step profiles narrow the effective inventory **before** Cognis chooses a
provider-specific discovery strategy.  The profile decides two things:

- which tools remain eligible for the current step
- which of those eligible tools are visible by default

Mode semantics:

- `soft` narrows the default-visible tool surface to the profile matrix plus
  explicit overrides, while keeping the searchable inventory broad for that step
- `hard` narrows the searchable inventory too, so discovery cannot escape the
  hard-approved subset
- agent/executor/runtime tool assignment remains the outer hard boundary in all
  cases

Deferred tools are tools Cognis may keep out of the current visible surface
until the model searches for them or explicitly loads a skill that exposes
them. Typical deferred candidates are MCP tools, Intaris MCP tools, and
skill-defined tools that are not attached by default.

### Skill Activation Discovery

`skill_load` is a controller-managed discovery path in its own right.

1. The model loads a skill summary into protected context with ``skill_load``.
2. If the skill version declares tool summaries that resolve to tool ids,
   Cognis activates those ids immediately.
3. If the skill has no declared tool ids, Cognis asks the configured
   ``classifier`` model to choose zero or more tool ids from the current hidden
   searchable inventory.
4. Activated tool ids are cached for the current session and included in later
   model-facing visibility calculations.

The classifier path is conservative by design. Empty results are valid and are
cached per session by ``(skill_id, content_hash)``.

| Provider / Setup | Strategy | Hidden tool discovery | Implications | Preferred |
|---|---|---|---|---|
| Anthropic with `supports_defer_loading=true` | `anthropic_defer_loading` | Deferred tools stay in the tool array with `defer_loading=true`; controller `search_tools` may still be available | Best Anthropic path; compact default surface and cache-friendly | Yes |
| OpenAI Responses with native namespaces + allowed-tools support | `openai_responses_tool_search` | Native `tool_search` plus namespace-deferred tools; core tools restricted with `allowed_tools` | Best intended OpenAI path; smallest advertised surface and strongest cache stability | Yes |
| OpenAI Responses without namespace tools but with allowed-tools support | `openai_responses_flat_tool_search` | Native `tool_search` plus flat deferred tools with `defer_loading=true` | Similar to the preferred OpenAI path, just less structured | Yes |
| OpenAI Responses downgraded from native tool search | `openai_responses_controller_search_fallback` | Controller `search_tools` discovers deferred tools from the step-filtered inventory | Keeps Responses transport but discovery becomes controller-managed; slightly worse cache stability and first-search UX | Fallback |
| OpenAI Responses with no deferred/search path left | `openai_responses_full_inventory` or `openai_responses_full_inventory_no_defer` | No discovery; everything is visible up front | Works, but increases tool-surface clutter and model confusion for large inventories | Avoid for large inventories |
| Generic chat-completions / Responses-disabled providers | `generic_search_tools` | Controller `search_tools` builtin only | Universal compatibility path; weaker than native deferred search but still respects step profiles | Fallback |
| Gemini and other providers without native deferred tools | `generic_search_tools` | Controller `search_tools` builtin only | Best available path; no provider-native defer/search support | Fallback |

#### Native OpenAI downgrade cache

When a provider/model rejects native OpenAI Responses `allowed_tools` /
`tool_search` parameters, Cognis marks that `(provider_id, model)` pair as
broken for native tool search and uses the controller fallback strategy for the
rest of the process lifetime.  The cache is cleared on restart or when the
provider configuration is updated.

### Tool Exposure Flow

```
1. Build full effective inventory (all sources, all tools)

2. Classify tools:
   - Core: builtin, executor-native, web → always loaded
   - Deferred: MCP, Intaris MCP, overflow → loaded on demand

3. Resolve the step profile:
   - Filter the eligible inventory (`hard` narrows search, `soft` keeps search broad)
   - Compute the default-visible subset for the step

4. Detect provider capabilities:
   - OpenAI gpt-5.4+: prefer Responses API native tool_search
   - Anthropic: use defer_loading + optional controller `search_tools`
   - OpenAI downgraded / unsupported: use Responses controller fallback
   - Other: use generic controller `search_tools` fallback

5. Build model-facing tool set:
   - Sanitize all names for provider compatibility
   - Apply provider-specific deferred loading flags
   - Build alias map (model-visible name → internal identity)
   - Apply cache hints (tool-level cache_control for Anthropic)

6. On tool call from model:
   - Reverse-map model-visible name to internal identity
   - Dispatch via ToolRouter using internal identity + source metadata

7. On search_tools call:
   - Search full effective permission-filtered inventory
   - Return matching tool schemas
   - Inject discovered tools into next turn
```

### Duplicate Tool-Call Safety

Tool-call correlation ids identify protocol items; they do not make side
effects idempotent. Cognis therefore applies provider-neutral duplicate safety
before executor dispatch:

1. Exact duplicates in one assistant response are collapsed by canonical tool
   name and JSON arguments, ignoring provider call ids.
2. Successfully executed non-read-only calls are recorded for the current turn.
   An exact repeat in a later LLM cycle is returned to the model as a synthetic
   skipped result and never reaches the executor. Failed calls and read-only
   calls remain retryable.
3. User retries, managed-conversation retries, and automatic continuations seed
   their ledger from the source turn, preventing a new turn id from bypassing
   the guard after the source turn already produced side effects. On controller
   restart or in-memory LRU eviction, Cognis reconstructs the transitive retry/
   continuation lineage from Intaris tool and system events across backing
   sessions.
4. The ledger is conversation-scoped and bounded in memory. It never suppresses
   calls across unrelated conversations or ordinary independent user turns.
   Intaris tool-call events carry a content-safe canonical argument fingerprint
   computed before model-visible argument bounding/truncation; raw full arguments
   are not duplicated into logs or Cognis storage.
5. Responses streaming must preserve the one-output-item/one-tool-call
   invariant. Multiple complete JSON objects received on one stream index are a
   corrected/divergent replay of one logical call, not parallel calls; Cognis
   keeps one call with the original id. Genuine parallel calls use distinct
   stream indexes.

The executor retains its same-turn duplicate check as defense in depth, but the
controller guard is authoritative because retries and continuations use new
turn ids.

### Prompt Caching Strategy

Prompt caching is critical for cost efficiency.  The architecture must
preserve cache hits across turns within a session.

**Current provider caching behavior:**

| Provider | Mechanism | Granularity | Tool caching |
|----------|-----------|-------------|--------------|
| OpenAI | Automatic prefix matching | 128-token increments from 1024+ | Tools are part of cacheable prefix |
| Anthropic | Explicit ``cache_control`` breakpoints (up to 4) | Per-block | Tools cacheable with ``cache_control`` |
| Gemini | Implicit + explicit context caching | 1024-4096 token minimum | Part of cached content |

**Cache-preserving design rules:**

1. **Keep the ``tools`` array stable across turns.**  Do not add/remove tools
   between turns within a session.  Use ``allowed_tools`` (OpenAI) or
   ``defer_loading`` (Anthropic) to vary visibility without changing the array.

2. **Place static content first.**  Order: tools → one consolidated immutable
   system message (identity → runtime instructions → memory instructions →
   core memories → skills metadata/guidance → continuation summary) → frozen
   project context → stable environment → history → user message → volatile
   tail reminders.

3. **Use multiple cache breakpoints for Anthropic.**  Recompute up to four
   breakpoints per cycle: last tool schema, immutable prefix, end of prior-turn
   history, and last message of the current request. `session.anthropic_cache_ttl`
   defaults to `5m`; `1h` applies only to tools/prefix and requires the extended
   cache TTL beta header.

4. **Use ``prompt_cache_key`` for OpenAI** when available, to improve routing
   stickiness across requests with shared prefixes.

5. **Deferred tools do not break cache.**  On both OpenAI (tool_search) and
   Anthropic (defer_loading), discovered tools are injected at the end of
   context or inline in conversation, not in the cached prefix.

6. **The generic ``search_tools`` fallback may break cache** if it injects
   new tools into the ``tools`` array on subsequent turns.  Mitigate by
   keeping injected tools in a separate promoted section at the end of the
   array on sorted non-defer paths, and using ``allowed_tools`` to restrict
   without array changes.

**Token budget accounting:**

Tool schemas consume tokens from the static budget.  The ``ContextAssembler``
must account for tool schema tokens when computing the dynamic budget for
messages and memory.  Core tools are a fixed cost; deferred tools are zero
cost until discovered.

## Skill System

Skills are DB-managed instruction + tool bundles with import/export support.
Agents can also manage skills via a built-in LLM tool.

Cognis remains compatible with the official `SKILL.md` style used by other
harnesses. A skill may therefore be:

- an instruction-only bundle that is loaded inline
- a bundle with tool recipes
- an instruction bundle that also declares reusable workflow material for the
  workflow composer

```python
class Skill(BaseModel):
    skill_id: str
    name: str
    description: str
    instructions: str              # Markdown, injected into context
    tools: list[ToolDefinition] = []
    prompt_templates: dict[str, str] = {}
    steps: list[dict[str, Any]] = []
    workflow_templates: list[dict[str, Any]] = []
    source: str                    # "db", "file"
    attach_to_all_agents: bool = False
    owner_email: str | None = None
    tags: list[str] = []
```

### DB Schema

```sql
CREATE TABLE skills (
    skill_id    TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    description TEXT,
    instructions TEXT NOT NULL,
    tools       JSON,              -- list of ToolDefinition dicts
    prompt_templates JSON,
    steps       JSON,
    workflow_templates JSON,
    tags        JSON,
    auto_load   INTEGER NOT NULL DEFAULT 0,  -- internal storage for attach_to_all_agents
    owner_email TEXT REFERENCES users(email),
    created_at  TIMESTAMP NOT NULL,
    updated_at  TIMESTAMP NOT NULL
);
```

### Skill Management

Skills are managed via:
1. **API** — CRUD endpoints at `/api/v1/skills`
2. **UI** — Tools & Skills page, Skills tab
3. **Import/Export** — YAML format for GitOps workflows
4. **LLM Tool** — `skill_write` built-in tool for agent self-management

The import/export surface preserves plain `SKILL.md` compatibility. Cognis-only
workflow extensions such as `steps:` and `workflow_templates:` are optional.

### Runtime Model

Skills have three distinct runtime states:

1. **Discoverable** — all visible skills for the user. Unattached ones are only
   counted in the prompt, found with the always-exposed `skill_search` core
   tool, and loaded explicitly with `skill_load`.
2. **Attached** — skills selected on the agent plus skills attached to all
   agents. Only these are listed in the prompt catalog, with `skill_id`, name,
   and description (capped at 1,024 characters). Attached skills whose
   instructions are auto-loaded are omitted from the catalog.
3. **Forced/Inherited** — workflow or delegation scoped attachments that can be
   injected into a sub-session explicitly.

Skills may also participate in three execution shapes:

1. **Inline guidance** — `instructions` loaded into a direct turn or
   `system:general-task` execution.
2. **Workflow fragment** — `steps:` consumed by `system:workflow_composer` as
   reusable step material.
3. **Workflow skeleton** — `workflow_templates:` consumed by the composer as a
   full-process starting point.

If a visible skill has no declared `steps:`, Cognis may still derive step
fragments on demand through the hidden `system:skill_decomposer` agent. That
derived structure is advisory to the composer and is never written back unless a
user or agent explicitly saves it.

Discoverability and attachment are intentionally separate. Agents should
choose from the prompt catalog or `skill_search` results and call `skill_load`
directly rather than browsing with `skill_list`. `skill_search` is a dedicated
skill tool rather than a `search_tools` result type: it returns skills, which
are loaded with `skill_load`, not tool handles invoked through `call_tool`.

Skill mutations by an agent (`skill_write`, `skill_patch`, asset changes,
`skill_import_url`, `skill_restore_version`) attach the skill to the acting
persistent agent; delegated `system:*` specialists act for their parent.
The `agent_skill_usage` table stores content-free per-agent load counters,
the last load time, and loads that followed a `skill_search` result. These
drive advisory curation hints only.

### Deferred Skill Tools

Skill-defined tools participate in the same deferred tool exposure model as MCP
tools:

- Attached skills start with their tool IDs marked as discovered.
- Discoverable but unattached skills expose their tools only after `skill_load`.
- Prompt metadata stays compact and cache-friendly because full skill content is
  loaded on demand.
- Provider-specific deferred loading (`defer_loading`, `search_tools`, future
  `allowed_tools` restrictions) controls visibility without changing the stable
  tool inventory.

This lets Cognis keep all visible skills loadable while still encouraging
explicit skill loading and preserving prompt caching.

### Cloud-Native Skill Storage

Skills are managed as Cognis records backed by the database and artifact store.
System skills may be seeded from packaged application resources on bootstrap,
but skills are not discovered from editable controller filesystem directories.

`skill_load` resolves the current authorized version, releases its DB session,
and sends that immutable bundle through `skill.assets.prepare` on the active
authorized executor connection. This authenticated controller-only RPC does not
trust configure-time `skill_manifests` and is not an LLM-callable tool. It returns
version-pinned paths, hashes, and asset identities for the result and protected
skill context. Preparation errors prevent successful loading/activation; no
scratch fallback is allowed. `skill_asset_materialize` is retired.

Executors cache verified assets under their Cognis data directory, isolated by
actor, skill, and version. Relative bundle layout is preserved. Cache reads
verify hashes; writes are atomic and use no-follow directory/file descriptors.
Retrying preparation is idempotent. Cancellation belongs to the calling load;
an interrupted cache fill can leave only verified version-scoped files, and a
subsequent load safely repairs/reuses them. Auto-injected instructions without
paths explicitly require a load before asset-only helper use.

Executable recipes still stage the specific assets they need temporarily.
The controller remains the source of truth for skill metadata, versions, and
assets. General authoring guidance requires portable versioned resources and
publication → load → clean execution evidence.

## Complete Tool Call Flow

```
0. Tool exposure layer prepares model-facing tool set:
   - Assemble full effective inventory
   - Classify into core (always loaded) and deferred (discoverable)
   - Apply provider-specific mechanisms (tool_search, defer_loading, allowed_tools)
   - Sanitize names for provider compatibility
   - Build alias map (model-visible name → internal identity)

1. LLM generates tool_call(name, arguments)
   - Mixed batches are handled per-call. If one sibling has malformed JSON
     arguments, the assistant transcript still records every emitted call; valid
     siblings execute normally, and malformed siblings receive synthetic
     `is_error=true` tool results with bounded raw-argument previews.
   - `finish_reason=length` with tool calls is treated as an incomplete batch:
     emitted calls are rejected with a synthetic "output limit hit mid-call"
     result and the model must re-issue them.

1a. Reverse-map model-visible name to internal identity via alias map

2. Tool Router categorizes:
   a. Orchestration → handle as session op (step 2a)
   b. search_tools → search full inventory, return matches (step 2b)
   c. Intaris MCP → Intaris proxy (step 2c)
   d. Executor-native or Local MCP → evaluate and dispatch (steps 3-6)

   2a. Decision Engine approves/modifies → create session, start agent loop
   2b. Controller searches effective inventory → returns tool schemas to LLM
       → LLM may call discovered tools on next turn
   2c. Intaris evaluates + executes → return result to LLM
       (uses source.server_name + source.raw_tool_name for dispatch)

3. Permission evaluation:
   a. Non-bypassable → always Intaris
   b. ALLOW → auto-approve (skip Intaris)
   c. DENY → block immediately
   d. EVALUATE → Intaris evaluation

4. Intaris evaluation:
   a. APPROVE → continue to step 5
   b. DENY → denial + reasoning back to LLM
   c. ESCALATE → pause, notify user, wait (with timeout)

5. Tool execution on executor:
   Controller sends tool.execute → Executor runs tool → returns result
   (Executor checks native handlers first, then MCP dispatch)

6. Record events to Intaris:
   tool_call event + tool_result event + evaluation event

7. Result sanitization (see Trust Model below)

8. Sanitized result returned to LLM for next iteration
```

## Trust Model for Injected Content

### Problem

Tool results, Mnemory recall content, and Intaris event content can carry
**untrusted data** into the LLM context. A tool reading a web page, file, shell
output, browser page, channel message, or API response may return content
crafted to manipulate the LLM (prompt injection via tool output). Pre-execution
guardrails (Intaris evaluate) do not protect against this — they evaluate the
*request*, not the *result*.

### Defense Layers

#### Layer 1: Capability-Based Structural Isolation (MVP)

`ToolDefinition.content_trust` declares whether result content is trusted or
untrusted. Trusted tool confirmations and summaries are not wrapped. Untrusted
tools are wrapped before injection into the model context.

Default trust is `trusted`; tools that expose external or user-controlled
content must opt into `content_trust="untrusted"`. Required untrusted
categories include:

- file content returned by `read`;
- shell output from `bash` and `bash_output`;
- web and browser tools;
- all MCP tools (including legacy `local_mcp` / `intaris_mcp` sources);
- channel-derived content.

Trusted categories include filesystem write/edit confirmations, `glob`/`grep`
summaries, `list_directory`, LSP diagnostics, todo/workflow/system built-ins,
and other controller summaries that do not directly carry untrusted external
content. A trusted tool may tighten a specific result to untrusted via result
metadata; `grep` does this for `output_mode="content"` because match snippets
include file text, while `files_with_matches` and `count` remain trusted
summaries.

Mark untrusted content with clear boundaries in the context:

```python
def _wrap_tool_result(result: str, content_trust: str) -> str:
    if content_trust == "trusted":
        return result
    neutralized = result.replace("</tool_result>", "<\u200b/tool_result>")
    return (
        "<tool_result trust=\"untrusted\">\n"
        f"{neutralized}\n"
        f"</tool_result>"
    )

def _wrap_memory_context(self, recall: RecallResult) -> str:
    return (
        "<memory_context trust=\"untrusted\">\n"
        f"{self._format_memories(recall)}\n"
        "</memory_context>"
    )
```

This is not a security boundary — the LLM can still be influenced — but it
makes the trust level explicit in the prompt, avoids laundering arbitrary
external text through a trusted tool name, and neutralizes wrapper breakouts.
The wrapper intentionally omits a `name` attribute to avoid increasing the
authority of the content by naming a trusted tool, and embedded closing tags are
neutralized with a zero-width separator (`<\u200b/tool_result>`).

#### Layer 2: Output Size Limits, Projection, and Context Management

Tool output management is split between durable storage and model-facing
projection. Full outputs are recoverable by handle; the prompt receives a
budgeted view sized for the current model, phase, and pressure mode.

**2a. Per-tool truncation** at execution time:
- Each executor tool applies its own output cap (foreground shell: head 100K
  chars + tail 300K chars, web_fetch: 500K, read: 2K lines × 2K chars with
  over-budget limit reduction, grep: 200 files with configurable per-file
  content caps).
- The shared tool output presentation layer applies **middle-truncation**
  to `max_result_size` (default 50,000 chars): the head and tail of the
  output are preserved, the middle is removed, and head/tail cuts snap to
  nearby newline boundaries with a bounded scan. The truncation marker and
  metadata include recovery details only when full output is available. Anchor
  recovery instructions are emitted only when anchor metadata exists.
- Full output (after executor truncation, before context truncation) is
  saved to the **ToolOutputStore** on the controller's local filesystem
  (`{COGNIS_DATA_DIR}/tool-outputs/{call_id}.txt`). A background maintenance
  service periodically enforces both the configured TTL and storage size cap;
  storage scans never block application startup or the controller event loop.
- While a streaming tool is still running, raw chunks are also written to a
  bounded **live tool-output spool** keyed by conversation/session/call. The
  spool stores chunk index, offset, stream, and text with byte/chunk limits and
  an expiry refreshed while the call is active. It exists only for live UI
  paging and short completion continuity; completed output recovery continues
  to use the ToolOutputStore as the canonical source.

**2b. Budgeted projection** before model calls:
- Projection is invisible to users and logged for operators. It must not emit
  chat-visible "pruned output" notices during normal operation.
- The controller derives an internal projection policy from the model context
  window, effective prompt budget, and pressure mode. Large context windows are
  used as safety margin first: normal prompts target roughly 90K tokens for
  128K models, 180K for 272K models, 250K for 400K models, and 300K-320K for
  1M models unless the active turn needs a burst.
- Projection separates **cross-turn replay** from **within-turn evidence**.
  Cross-turn history remains verbatim while the full prompt is below the steady
  target. Above that target, the controller computes the required token savings
  and projects only enough oldest fully recoverable tool groups to return toward
  the target; fixed group counts remain fallback policy inputs, not a reason to
  discard low-pressure context. Within-turn tool output may use a much larger
  burst budget so the agent can finish the current reasoning path without
  repeatedly re-reading the same output.
- Projection modes are `normal`, `pressure`, and `critical`. Under pressure,
  completed same-turn tool results may be replaced with recoverable
  placeholders, but unresolved tool calls, explicitly protected outputs, and the
  newest completed same-turn evidence remain protocol-safe.
- Tool-call arguments above the projection policy threshold are cleared in
  compacted zones. The threshold is 6,000 characters; when arguments are
  structured objects, projection preserves a safe head such as `file_path` and a
  500-character content preview before clearing the rest. Intaris events and the
  ToolOutputStore remain unaffected.
- Delegation result replay is bounded across turns. Current-turn injection can
  include the full selected child result, but cross-turn replay keeps only a
  6,000-character head plus recovery-handle text; those replay messages are
  marked prunable so the projector can replace them with compact recovery
  placeholders under pressure.

**2c. Post-turn cache pruning** after each agent turn:
- The session cache records which older tool outputs can be represented by
  placeholders on future replay. This is an optimization and diagnostics input,
  not the semantic source of truth. Cold rebuild from Intaris must still produce
  a bounded projection from persisted event metadata.
- Post-turn pruning is log-only from the user's perspective. `/context` and
  `/info` expose the last projection policy and token budgets for diagnostics.

**2d. Exploration tools** for recovering cleared/truncated output:
- `read_tool_output(call_id, offset?, limit?)` — paginated line-by-line
  read from the ToolOutputStore, similar to the file read tool.
- `search_tool_output(call_id, pattern, context_lines?)` — regex search
  with context lines, similar to grep.
- These are controller-side built-in tools (read-only, no guardrails
  evaluation needed).
- The chat UI uses `GET /api/v1/conversations/{conversation_id}/tool-outputs/{call_id}`
  for generic paged viewing. The endpoint serves live-spool pages while a call
  is running, stored ToolOutputStore pages after completion, and falls back to
  the bounded event preview when no recoverable source exists.

**Storage layers:**

| Layer | Content | TTL | Purpose |
|-------|---------|-----|---------|
| LLM context | Budgeted projection: rich active evidence + placeholders | Current call | What the LLM reasons over |
| Intaris event | Middle-truncated preview (~50K) + presentation metadata | Session lifetime | Compaction, audit, replay, recovery metadata rehydration |
| ToolOutputStore | Full executor output | 24h (configurable) | LLM exploration via tools (filesystem or S3 backend) |
| Live tool-output spool | Bounded raw streamed chunks | Short TTL refreshed while running | UI live/full-output drawer paging for running calls |
| WebSocket/SSE | Middle-truncated transport preview + metadata | Ephemeral | UI display |
| Active tool output snapshot | Bounded presentation output | Hours | Refresh/reconnect hydration while a tool is running; backed by Redis when available with in-memory fallback |

#### Layer 3: Post-Execution Content Evaluation (Phase 2)

For high-risk tool categories, pass tool results through Intaris for
content evaluation before injecting into the LLM context:

```
Tool executes → result
  │
  ├─ Low-risk tool (read_only=true, known schema) → inject directly
  │
  └─ High-risk tool (bash, web_fetch, external API) →
       Intaris content_evaluate(result) →
         CLEAN → inject
         SUSPICIOUS → inject with warning prefix
         DANGEROUS → replace with "[content blocked by safety policy]"
```

This requires an Intaris enhancement (`POST /api/v1/content_evaluate` or
similar). Not MVP, but the architecture should accommodate it.

#### Layer 4: Structured Output Contracts (Ongoing)

Where possible, prefer tools that return structured data (JSON with a
known schema) over raw text. Structured outputs have a smaller injection
surface because the data follows a predictable format.

### MVP Requirements

- Structural wrapping of untrusted tool results and memory context (Layer 1).
- Tool output size limits with truncation (Layer 2).
- Layers 3 and 4 are Phase 2+ improvements.
- The system prompt should include a note that tool results and memory
  content are untrusted external data.
