# Changelog

All notable changes to this project will be documented in this file.

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
