# Changelog

All notable changes to this project will be documented in this file.

## [0.11.0] - 2026-07-05

### Added

- Added the Chat v2 realtime sync architecture with runtime overlay frames, REST snapshot/sync/backfill endpoints, and invariant coverage for ordering, deduplication, reconnection, and final-state preservation.
- Added a deterministic mock-LLM E2E harness, browser replay scenarios, and expanded golden streams for streaming, tool-output, reconnect, and multi-phase assistant flows.
- Added Matrix channel support and richer channel/account controls, including group-room handling, authenticated media download, thread overrides, and improved formatting.
- Added Anthropic subscription OAuth support, executor-routed inference fields, live context-usage streaming, chat display preferences, and rich Chat v2 tool/thinking/delegation rendering.

### Changed

- Reworked chat rendering around native Chat v2 projection, grouped activity/tool segments, stable runtime ordering, scroll stability, and consolidated timeline/session-log rendering.
- Improved MCP OAuth, executor reconfiguration, provider routing, Responses/Anthropic request handling, prompt-cache assembly, and tool-call loop hygiene.
- Improved task board/detail performance, PWA update handling, sidebar/conversation state reconciliation, structured question UX, and mobile chat controls.
- Expanded backend and UI tests across Chat v2, channels, MCP OAuth, provider routing, notifications, executor runtime, and E2E streaming scenarios.

### Fixed

- Fixed Chat v2 streaming races that caused duplicate, disappearing, stale, or incorrectly ordered messages across tools, thinking blocks, compaction, reconnects, and refreshes.
- Fixed notification retry idempotency, managed conversation delivery, channel attachment handling, Matrix room classification, and known Matrix thread follow-ups.
- Fixed LLM/provider edge cases for Responses transcript persistence, cache breakpoint lists, provider kwargs sanitization, stream failure continuations, and model discovery.
- Fixed executor/tool hardening issues around skill asset materialization, rejected OAuth-token recovery, MCP server changes, artifact refs, file diagnostics freshness, and legacy MCP category disables.

## [0.10.0] - 2026-06-21

### Added

- Added MCP OAuth device-code flow support with token refresh serialization and regression coverage.
- Added contextual controller aliases for tools and deterministic workflow step specifications.
- Added backend filtering for expired one-shot schedules and UI grouping for conversation history by activity date.
- Added markdown output anchors and recovery support for large tool-output navigation.

### Changed

- Improved chat timeline projection, live-state reconciliation, sidebar projection loading, and conversation open performance.
- Refined structured question prompts, inline answers, pagination, and pending-question collapse behavior.
- Tuned workflow and software-development prompts, delegated language guidance, and background activity reminders.
- Improved managed-conversation maintenance, stale conversation cleanup, and reusable managed-chat reminders.

### Fixed

- Fixed streamed context overflow recovery, native attachment preservation, and retained assistant/user timeline state across replay and refresh.
- Fixed MCP OAuth redirect URI handling and token refresh serialization.
- Fixed runtime completion notifications, stale managed conversations, and delegated specialist/system-agent selection safeguards.
- Fixed markdown output anchor deduplication and tool-output recovery anchors.

## [0.9.0] - 2026-06-14

### Added

- Added runtime profiles and per-agent execution profile controls across agents, schedules, workflows, APIs, bootstrap, migrations, and UI forms.
- Added system tool override management for agents, including MCP/system tool toggles and regression coverage.
- Added certified Signal CLI runtime management with install validation, direct JSON-RPC receive mode, and receive-mode tests.
- Added hidden-user projection support for provider controller notices and expanded Anthropic proxy/tool discovery compatibility.

### Changed

- Improved agent loop, workflow engine, tool exposure, context assembly, and delegation routing to respect profile-scoped execution and conversation surfaces.
- Improved message projection and LLM provider handling for Anthropic-compatible proxies, tool argument schemas, deferred loading, and search tool discovery.
- Refined task, schedule, and workflow serialization surfaces for runtime profile metadata and execution selectors.
- Expanded UI and unit coverage for agent profiles, tool exposure, direct Signal receive mode, workflow runtime behavior, and provider projection.

### Fixed

- Fixed cross-agent profile leakage in delegated conversations and isolated conversation profiles by agent.
- Fixed classified tool execution metadata usage, channel read receipts, history cursors, and chat reset handling.
- Fixed Signal direct receive notification handling and JSON-RPC startup behavior.
- Fixed workflow evaluation evidence to include session tool events and deferred tasks while executors reconnect.

## [0.8.0] - 2026-06-10

### Added

- Added managed agent conversations with chat UI support, retry handling, parent notifications, and durable managed-conversation state.
- Added first-class workflow question sets, richer workflow tool rendering, evaluator retry todo reopening, and a conversation summary tool.
- Added MCP server group tool assignment, OfficeCLI document tools, local compose stack support, and proactive skill update guidance.
- Added session policy UI/API propagation, compaction progress cards, background work reminders, and richer chat timeline status indicators.

### Changed

- Improved Responses and LLM streaming ownership, reasoning-state preservation, replay behavior, and content-part gating.
- Hardened context projection, compaction recovery, same-turn pressure handling, memory recall preservation, and task context injection.
- Improved MCP OAuth initialization, token refresh, timeout handling, executor reconfiguration, and nested auth failure classification.
- Refined chat routing, sidebar state, active chat recovery, route-change handling, conversation switching, and selected-agent direct chat behavior.

### Fixed

- Fixed oversized tool argument handling, classifier retry loop bounds, streamed assistant commentary preservation, and final transcript isolation.
- Fixed managed queued message handling, async work handoff behavior, agent-work continuations, and parent notification for async managed conversation creation.
- Fixed local compose release validation, migration sanity checks, OfficeCLI validation, executor websocket reconfigure recovery, and release-blocking regressions.
- Fixed UI loops, compaction timeline persistence, read-state synchronization, active tool snapshots, and workflow/task rendering regressions.

## [0.7.0] - 2026-06-01

### Added

- Added MCP OAuth authorization support with dynamic client registration, OAuth settings in the tools UI, and persistent MCP OAuth storage.
- Added sticky agent-direct chats, cached conversation switching, chat forking during active turns, and long-lived chat idle checkpoint compaction.
- Added UI affordances for selected-agent chat shortcuts, degraded executor visibility, native patch preparation, backing-session counts, and step todo summaries.
- Added session policy propagation to Intaris and new task session policy metadata.

### Changed

- Improved direct chat lifecycle handling, sidebar history refresh behavior, notification state synchronization, and conversation attention indicators.
- Tuned Codex and ChatGPT streaming, provider retry diagnostics, context projection, compaction, and runtime recovery behavior.
- Improved executor reconnect handling, MCP transport isolation, browser session behavior configuration, and schedule/runtime routing.
- Expanded API, workflow, runtime, and UI test coverage for recent chat, executor, MCP, scheduler, and workflow behavior.

### Fixed

- Hardened native apply-patch retry handling, malformed empty patch rejection, custom tool input streaming, and degenerate response stream diagnostics.
- Fixed orphan live tool-output buffering, stale active tool snapshots, history chronology, cached refresh banners, and direct conversation session recovery.
- Fixed Intaris title waits for adoptable conversations, task/session policy typing, weighted todo progress calculations, and browser/web extraction edge cases.
- Restored release validation tests and improved release-blocking regression coverage.

## [0.6.3] - 2026-05-24

### Fixed

- Auto-continued chat turns after the tool-call ceiling is reached so tool-heavy turns can finish instead of stopping early.
- Allowed task creator agents to manage delegated tasks they created, preserving delegated task ownership metadata through APIs and queues.
- Inferred routing model capabilities more accurately for provider/model settings and routing UI.
- Hardened executor image generation and structured image tool results, including fallback image result URLs and provider-compatible output handling.
- Cleaned up implementation-stage tracking documentation.

## [0.6.2] - 2026-05-24

### Fixed

- Hardened compaction session rotation so provider identity and guardrail context survive rotated sessions.
- Preserved provider identity metadata through guardrail calls and added regression coverage for session rotation behavior.

## [0.6.1] - 2026-05-23

### Added

- Added explicit CRUD support for agent-specific tool assignments across runtime support, managed-agent tooling, documentation, and tests.

### Fixed

- Preserved the original artifact owner context when reading artifacts through routed tools so shared or delegated reads do not lose access to the source artifact.
- Allowed repeated idle continuations in the agent loop so silent or continuation-only turns can keep progressing instead of stopping after the first idle continuation.

## [0.6.0] - 2026-05-23

### Added

- Knowledgebase ingestion and retrieval with artifact-backed sources, native hybrid search, source context reads, per-agent assignments, CRUD tools, token-aware chunking, configurable per-knowledgebase chunking settings, and embedding model routing.
- Direct Codex transport and native Codex discovery/usage support, plus Claude Agent SDK executor integration, Claude model catalogs, setup flows, provider settings, and Anthropic-compatible base URLs.
- Plan/build chat modes, starred conversations, separate chat popup windows, same-conversation undo/redo, todo progress indicators, and richer context diagnostics.
- Background shell status reporting, executor reconnect and routing improvements, project metadata loading for path-scoped agents, and channel context exposure to agents.
- Paged full tool-output viewing, streamed tool-output snapshot preservation, visible tool select matches, improved browser fetch consent handling, and Reddit JSON fetch fallback.

### Changed

- Reworked context projection and compaction around pressure-aware tool output budgeting, provider-overflow recovery, canonical projection, bounded compaction recursion, and more stable within-turn re-projection.
- Refined LLM provider behavior by removing Responses continuation dependence, dropping the Claude Code SDK provider, improving ChatGPT/Codex stream caching and idle recovery, and making model/provider routing more explicit.
- Improved conversation loading and live chat reconciliation with cached switches, authoritative reload boundaries, stable event state, preserved queued messages, and clearer recovery notices.
- Tightened runtime/delegation recovery with preserved tool runtime metadata, async delegation workspace context, saved child-result salvage, workflow log exposure, and explicit task slash-command routing.
- Strengthened mobile and UI behavior around keyboard safe areas, history ordering, fixed headers, mode markers, context controls, and sidebar todo visibility.

### Fixed

- Hardened knowledgebase tool exposure, assignment access, structured tool results, Docker dependencies, metadata ingestion, hybrid search, and embedding route ordering.
- Stabilized direct Codex generate streaming, provider stream diagnostics, idle LLM recovery, recoverable model failures, custom Responses call IDs, and provider credential scoping.
- Fixed stale projection tool prefixes, compaction session rebuilds, historical tool-output projection caps, tool argument validation, local tool argument rejection, and malformed select aliases.
- Corrected executor pin expiry, executor switching/rebinding, foreground shell timeout cleanup, LSP initialization/default analysis bounds, and remote provider target validation.
- Preserved chat timelines, live tails, user message identity, chat filters, activity ordering, attachment handling, markdown rendering, and mobile overlay safe areas.

### Documentation

- Added and updated knowledgebase source-context guidance, long-running bash/background command guidance, task agent ownership notes, additional executor handling, and provider/context diagnostics documentation.

## [0.5.0] - 2026-05-08

### Added

- Voice mode with TTS, web STT, conversation playback, mobile recording flows, sentence-buffered audio, and per-agent/system voice routing.
- Cognis conversation search with local and server-backed matching, search highlighting, in-conversation navigation, and conversation search tools.
- Multi-executor agent support, workflow same-executor routing, executor configuration UI/API matrix, and non-expiring revokable remote executor tokens.
- Chat continuation forks, queued web messages, active turn indicators, file diff display for tool edits, and richer push notifications.
- Browser attachment and interaction tools, iframe payment input improvements, and ChatGPT subscription OAuth provider support.

### Changed

- Made coding and research workflows more review-driven, with clearer revision loop prompts, stronger finalization boundaries, and reduced planning/tool overhead.
- Improved delegation contracts and task session logs, including slim system-agent sub-sessions and parallel delegation reliability updates.
- Refined PWA/mobile viewport handling, safe-area behavior, notification routing, task board column collapse, and settings/provider mobile layouts.
- Optimized context assembly and agent-loop latency by replaying history attachments natively, pruning post-step state, and parallelizing safe classified tools.

### Fixed

- Hardened queued chat sends, attachment-only message deduplication, assistant event `turn_id` persistence, PDF attachments, and stale escalation prompt expiry.
- Stabilized voice playback, microphone teardown, mobile keep-awake behavior, TTS caching, and conversation-mode text suppression.
- Fixed delegated result extraction, delegated deliverable prioritization, sub-session turn limits, and delegation failure handling.
- Improved workflow routing, retry output preservation, automatic workflow selection, provider JSON fallback, and native `apply_patch` replay safeguards.
- Fixed conversation search scoring and exact-match preservation, task recovery after restart, schedule definition exposure, recall payload limits, and uploaded filename sanitization.

### Documentation

- Added and completed voice mode, conversation search, and multi-executor implementation specifications.

## [0.4.0] - 2026-04-29

### Added

- Browser stealth defaults, Patchright runtime support, autoconsent handling, input humanization, and fingerprint hardening.
- Web push notifications, preferred channel delivery defaults, managed agent tooling, and task/schedule interaction mode overrides.
- Project workflow and revision management, including project workflow bindings and the completed project management UI.
- Expanded web tooling with SearXNG, browser fetch fallback, DIY crawl/map support, structured extraction metadata, media extraction, headed fallback, and binary fetch artifacts.

### Changed

- Improved browser session lifecycle, navigation waits, headed/headless runtime behavior, persistent manager handling, and browser auto-install safeguards.
- Refined mobile and workflow UI navigation, task metadata panels, board filters, avatar/workflow presentation, bottom navigation spacing, and unfocused chat notifications.
- Strengthened shared-agent runtime ownership, memory owner preservation, discovered tool exposure persistence, and lifecycle event recording.

### Fixed

- Stabilized in-flight assistant stream recovery, session history hydration after restart, markdown fence rendering, fetch fallback retry behavior, and chat scroll preservation.
- Fixed executor reconfiguration teardown, default executor workdir selection, stalled LLM stream handling, JSON fallback handling, rejected todo write preservation, and inline browser OTP challenges.
- Corrected PostgreSQL channel delivery defaults, schedule preferred channel delivery exposure, memory delete discoverability, and shared agent grantee runtime support.

### Documentation

- Added the Projects, step metadata gating, and human revision specification.
- Polished public launch materials, refreshed launch diagrams and README screenshots, and removed OpenClaw references.

## [0.3.0] - 2026-04-26

### Added

- Workflow deliverables, composition and promotion flows, decomposed skill workflow sources, step tool profiles, task reruns, and artifact save/download/discovery tools.
- Mobile-first PWA and chat UX overhaul, including installable assets, safe-area handling, navigation drawers, edge swipes, chat virtualization, attachment previews, copy buttons, themes, and agent/profile affordances.
- Assistant thinking/reasoning support, Responses immutable-prefix handling, prompt critical rules, model routing thinking effort, and native `apply_patch` editing support.
- Agent sharing and multi-user ownership controls, user-scoped private MCP management, linked skill runtime tool bindings, and tightened shared-resource authorization.

### Changed

- Refined tool exposure around promoted tools, deferred discovery, OpenAI tool handling, step profile defaults, and model-family-specific edit tools.
- Moved the web app to browser sessions and improved runtime selection, executor binding, local executor restrictions, and MCP HTTP failure containment.
- Improved workflow runtime contracts, lifecycle invariants, session recovery, context compaction, attachment shaping, and task/status metadata.

### Fixed

- Stabilized chat turn ordering, phase replay, fresh conversation handling, queued input boundaries, task prompts, reasoning block rendering, tool artifact replay, and direct step profile application.
- Hardened workflow gates, evaluators, delegated deliverables, step completion metadata, timeouts, retries, leaked step-slot recovery, and default task delivery.
- Improved UI reliability across mobile, iOS PWA keyboard/safe-area behavior, drawers, sidebars, task logs, scroll containment, onboarding, and stale service-worker updates.
- Fixed executor, LLM, memory, search, attachment, artifact, and tool-call recovery paths including streamed argument replay and native patch result replay.

### Documentation

- Added and updated specifications for deliverables, step profiles, workflow composition, agent sharing, harness stabilization, deployment, Docker, and Kubernetes executor examples.

## [0.2.0] - 2026-04-14

### Added

- Browser credentials and automation, including persistent Playwright profiles, Xvfb fallback, session discovery, and lower-level inspection controls.
- Executor HTTP transport support for MCP plus stricter `apply_patch` handling in executor tooling.
- In-chat task and workflow management tools, general task fallback, paused gate chat commands, and operator-guided gate resolution.
- Signal preview metadata and richer attachment context for channel delivery.
- Resettable system management skills and sharper routing support for follow-ups and chat turns.

### Fixed

- Browser session lifecycle, display readiness, snapshot targeting, and credential failure recovery across the executor/browser stack.
- Workflow evaluation, reviewer handoff, gate targeting, UI outcome routing, and evaluator content truncation.
- Executor and MCP degradation handling, shell resolution, fresh-read enforcement before file edits, and LSP/runtime auto-install behavior.
- Attachment replay preservation, Signal markdown/artifact handling, queue observer isolation, Intaris-first session syncing, and escalation recovery.
- Embedded documentation links, executor settings polling, prompt todo churn, and live session info visibility.

### Documentation

- Added and refined runtime, takeover, recording, and auto-routing specifications.
