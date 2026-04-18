# Specifications

| File | Content |
|---|---|
| `00-vision.md` | Project vision, design principles, phased delivery |
| `01-architecture.md` | System architecture, DB schema, session cache, package structure |
| `02-agent-model.md` | Agent definitions, personality, delegation, and runtime/execution model |
| `03-session-model.md` | Session model, turn lifecycle, context assembly, recovery, retention |
| `04-controller-executor.md` | Controller-executor separation, executor placement, and runtime hosting |
| `05-integrations.md` | Mnemory/Intaris/LLM/tool contracts with verified APIs |
| `06-tool-system.md` | Tool routing, permissions, MCP, trust model |
| `07-security-identity.md` | JWT auth, bootstrap, cross-service access, threat model |
| `08-federation.md` | Future federation design (A2A, DID) |
| `09-ui-ux.md` | SvelteKit UI, Typer CLI, WebSocket protocol |
| `10-api-spec.md` | REST + WebSocket API surface |
| `11-deployment.md` | Local/Docker/K8s deployment, env var reference |
| `12-mvp-roadmap.md` | 8-week implementation plan |
| `13-nfr-operations.md` | NFRs, SLOs, metrics, degraded modes, retention |
| `14-workflow-engine.md` | Workflow templates, step types, completion protocol, evaluation, gates |
| `15-browser-credentials.md` | Browser automation, credential records, auth request flows, and cloud-native executor behavior |
| `16-document-generation.md` | Internal document-generation pipeline and artifact flow |
| `17-agent-runtimes.md` | First-class agent runtimes, including executor-hosted Claude Code |
| `18-runtime-contract.md` | Normative runtime lifecycle, event, projection, and tool contract |
| `19-runtime-implementation-plan.md` | Phased implementation plan for first-class runtimes |
| `20-auto-routing-implementation-plan.md` | Deterministic agent/workflow auto-routing, execution envelopes, classifier fallback, and rollout plan |
| `21-workflow-deliverables.md` | Typed, versioned deliverables, `write_deliverable` tool, step_complete gate, and once-only channel delivery |
| `22-step-profiles.md` | Step profiles (`unrestricted`, `research`, `coding`), tool classification taxonomy, per-step overrides |
| `23-harness-stabilization.md` | Stabilization and refinement plan for harness correctness, capability parity, prompt caching, memory freshness, and operational resilience |
| `24-provider-stabilization.md` | LLM provider/model handling correctness: reasoning-effort translation, sampling-parameter stripping, capability-flag gating, deterministic provider resolution, Responses bridge hardening |
| `25-harness-polish.md` | Final harness polish pass: MCP image/resource passthrough, skill-load context protection, mid-stream recovery reversal, provider-native tokenizer, session-lock sweeper, EventBus dead-subscriber eviction, dynamic MCP nonexistent-tool prompt, workflow `reasoning_effort` validator |
| `26-llm-exposure-audit.md` | LLM-exposure auditing in Intaris: new `system_message`/`developer_message`/`context_snapshot` event types, per-turn audit of every LLM-exposed message, Intaris-anchored immutable prefix, removal of `memory_stale` and per-field TTL, hard-fail on missing core memories |
| `27-workflow-composer.md` | Workflow-first execution model: main-agent-owned workflow composition, ephemeral workflows, coding workflow family, skill step decomposition, and `compose_and_run_workflow` |
| `28-agent-sharing.md` | User-to-user agent sharing, polymorphic `agent_grants` table (user wired, group reserved), two-headed runtime identity, Mnemory `(user, owner)` memory keying, owner-configurable executor scope per share, and explicit "no admin bypass for user-owned resources" rule |
