# Stage 16: Executor-Native Tools, Tool Management UI, and Executor UI

**Status**: DONE

## Implementation Notes

- Added 10 executor-native tools: read, write, edit, apply_patch, multiedit, list_directory, glob, grep, bash, web_fetch
- New `executor` source type with priority 400 (between builtin 500 and skill 300)
- Tools & Skills page added as top-level nav item at `/tools`
- Executors tab added to Settings page with full CRUD, tool toggles, presets, label editor
- **Tool-to-executor assignment**: executors DB table with `enabled_tools` and `enabled_tool_groups`
- **Default executor**: created on first start with no tools enabled (explicit opt-in)
- **Executor selection**: agent config → explicit ID → label selector → default executor
- **Two-registry bug fixed**: per-turn factory now builds registries WITH handlers
- **Getting Started**: added "Configure executor tools" step
- Tool parameters with descriptions shown in expanded tool detail on Tools page
- Intaris MCP auto-discovery via `GET /api/v1/mcp/servers` on Intaris
- Agent form updated with Intaris MCP server selection (multi-select)
- Skills DB table with full CRUD API
- 27 new unit tests, all passing
- markdownify is an optional dependency for web_fetch HTML-to-markdown conversion

**Repo**: `cognis`
**Depends on**: Stage 15 (closure)
**Estimated effort**: 2-3 days

## Objective

After this stage, Cognis has:
1. Built-in developer tools (read, write, edit, bash, glob, grep, etc.) that work without MCP server configuration
2. A unified Tools & Skills page showing all tools by source with skill management
3. Executor status visibility in Settings
4. Intaris MCP server auto-discovery in the agent form
5. DB-managed skills with CRUD API

## Deliverables

### 1. Executor-Native Tools (`cognis/tools/executor/`)

New directory with 5 modules:

- `definitions.py` — Tool definitions and handler registry for all 10 tools
- `filesystem.py` — read, write, edit, apply_patch, multiedit, list_directory handlers
- `search.py` — glob, grep handlers
- `shell.py` — bash handler
- `web.py` — web_fetch handler

All tools return `ToolResult` and follow the `ToolHandler` protocol.
Write operations (write, edit, apply_patch, multiedit, bash) are `non_bypassable=True`.
Read operations (read, glob, grep, list_directory, web_fetch) are `read_only=True`.

### 2. Tool Registry Updates (`cognis/tools/registry.py`)

- Added `"executor": 400` to `SOURCE_PRIORITIES`
- Priority order: builtin(500) > executor(400) > skill(300) > local_mcp(200) > intaris_mcp(100)

### 3. Runtime Support Updates (`cognis/api/runtime_support.py`)

- `static_tool_definitions()` now includes executor-native tools
- Executor tools are available to all agents by default (opt-out model)

### 4. In-Process Executor Updates (`cognis/providers/executor/in_process.py`)

- `_build_runtime_handler()` now accepts `native_handlers` parameter
- Executor-native tools are dispatched before MCP tools
- `spawn()` loads native handlers via `executor_tool_handlers()`

### 5. Intaris MCP Discovery (`cognis/providers/guardrails/intaris.py`)

- `list_mcp_servers()` — calls `GET /api/v1/mcp/servers` on Intaris
- `list_mcp_tools()` — calls `GET /api/v1/mcp/tools` on Intaris
- Both methods added to `GuardrailsProvider` protocol

### 6. Skills DB Table and CRUD

- `SkillRow` ORM model in `cognis/store/models.py`
- Alembic migration `007_skills_table.py`
- Query helpers: `list_skills`, `get_skill`, `create_skill`, `update_skill`, `delete_skill`
- API routes in `cognis/api/routes/skills.py`: GET/POST/PUT/DELETE `/api/v1/skills`

### 7. New API Endpoints (`cognis/api/routes/tools.py`)

- `GET /api/v1/tools/executor` — list executor-native tools
- `GET /api/v1/tools/local-mcp/observed` — list cached observed local MCP tools
- `GET /api/v1/executor/status` — executor status and capabilities
- `GET /api/v1/intaris/mcp/servers` — auto-discover Intaris MCP servers
- `GET /api/v1/intaris/mcp/tools` — list normalized Intaris MCP tools

### 8. UI: Tools & Skills Page (`ui/src/routes/(app)/tools/+page.svelte`)

- Tab-based page: Tool Registry + Skills
- Tool Registry: browse all tools grouped by category with source-aware badges, search/filter, expandable details
- Skills: list, create, edit, delete skills with form UI
- Added "Tools" to sidebar navigation with Wrench icon

### 9. Executor Configuration Table and CRUD

- `ExecutorRow` ORM model in `cognis/store/models.py`
- Alembic migration `008_executors_table.py`
- Query helpers: `list_executors`, `get_executor_row`, `create_executor`, `update_executor`, `delete_executor`, `ensure_default_executor`
- API routes in `cognis/api/routes/executors.py`: GET/POST/PUT/DELETE `/api/v1/executors`
- Default in-process executor seeded on startup with no tools enabled

### 10. Executor Resolution Logic (`cognis/core/executor_resolution.py`)

- `is_tool_enabled()` — check if tool is enabled by name, wildcard, or group
- `filter_tools_by_executor()` — filter tool list by executor config
- `labels_match()` — k8s-style label matching (AND logic)
- `select_executor_for_agent()` — resolution: explicit ID → label selector → default

### 11. Two-Registry Bug Fix (`cognis/api/runtime_support.py`)

- `build_registry_with_handlers()` — builds registries WITH actual handler functions
- `_build_handler_map()` — combines system + executor handlers
- `_resolve_executor_config()` — loads executor config from DB for agent
- Per-turn factory now filters tools by executor enablement and builds handler-attached registries

### 12. UI: Executors Settings Tab

- Full CRUD: list, create, edit, delete executors
- Tool group toggles (category-based enablement)
- Individual tool toggles with visual feedback
- Quick presets: "Read-only tools", "All tools", "None"
- Label editor (k8s-style key=value)
- Default executor badge, status indicators

### 13. Getting Started Integration

- Added "Configure executor tools" step to readiness checklist
- `executor_tools_configured` readiness flag in diagnostics

### 10. UI: Agent Form Improvements

- Intaris MCP server selection (multi-select of auto-discovered servers)
- Renamed "MCP servers" to "Local MCP servers" for clarity
- `intarisMcpServers` added to `AgentFormState` and payload serialization

### 11. Spec Updates

- `06-tool-system.md` — Complete rewrite: executor source type, all 10 native tools, updated routing diagram, safety classification table
- `04-controller-executor.md` — Native tool dispatch in executor, updated architecture diagram
- `02-agent-model.md` — Opt-out tool availability, updated agent examples, skill system reference
- `09-ui-ux.md` — Tools & Skills page, Executors tab, updated route table
- `10-api-spec.md` — New endpoints for tools, skills, executor

## Acceptance Criteria

- [x] 10 executor-native tools implemented with handlers
- [x] All write tools are non_bypassable, all read tools are read_only
- [x] Executor source type in registry with correct priority
- [x] static_tool_definitions() includes executor tools
- [x] In-process executor dispatches native tools before MCP
- [x] Intaris MCP discovery endpoint proxied through Cognis API
- [x] Skills table with Alembic migration
- [x] Skills CRUD API endpoints
- [x] Tools & Skills page in UI with navigation and parameter display
- [x] Executors tab in Settings with full CRUD, tool toggles, presets
- [x] Agent form Intaris MCP selection
- [x] Executors DB table with tool assignment (name + group)
- [x] Executor selection: explicit ID → label selector → default
- [x] Two-registry bug fixed: registries built WITH handlers
- [x] Default executor seeded on startup with no tools enabled
- [x] Getting Started checklist includes executor tools step
- [x] 54 new unit tests passing (27 executor tools + 27 executor resolution)
- [x] 286 total unit tests passing
- [x] ruff check clean
- [x] mypy clean on changed files

## Key References

- `docs/specs/06-tool-system.md` — Tool system specification
- `docs/specs/04-controller-executor.md` — Controller-executor architecture
- `docs/specs/02-agent-model.md` — Agent model with tool config
- `docs/specs/09-ui-ux.md` — UI specification
- `docs/specs/10-api-spec.md` — API specification
- `cognis/tools/executor/` — Executor-native tool implementations
- `cognis/api/routes/skills.py` — Skills CRUD API
- `cognis/api/routes/tools.py` — Tool discovery and executor status API
- `ui/src/routes/(app)/tools/+page.svelte` — Tools & Skills page
- `tests/unit/test_executor_tools.py` — Unit tests
