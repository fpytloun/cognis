# Changelog

All notable changes to this project will be documented in this file.

<!-- towncrier release notes start -->

## [0.19.0] - 2026-11-12

### Added

- Automatically choose an eligible profile when a conversation starts, with Auto, Default, and explicit-profile choices and a configurable confidence threshold. Add per-agent session analytics controls independent of guardrails. The classifier route accepts native typed decisions or chat models; auxiliary_generation handles chat-only recap and workflow generation.
- Transfer artifact files over streaming HTTP with integrity checks and atomic downloads instead of embedding file bodies in control messages. Deterministic workflows can publish large files and retain compact artifact references.
- Share document lineages with named users and grant authorized agents read or write access across revisions. Revision history records each author, and conflicting writes do not overwrite a newer revision. Public comment links are opt-in and revocable; private links require authenticated access.
- Allow agents to contact Cognis users through the built-in app channel. Managed exchanges use normal chat and expose only delivered messages to recipients. Channel settings keep the app channel available, and managed conversations can select a project or use the target agent's default project.
- Add catalog support for GPT-6.1 Terra and Claude Fable 5.2, and recognize GPT Image 2.6 Flare and Sunburst image capabilities and prices. Preserve GPT-Image-2 generation and editing while reporting its upstream deprecated status.
- Add executor-hosted Python tool calling for conversations and reviewed workflow `program` steps, with live output and linked tool activity. Nested tools use normal authorization and approval. Writable calls remain subject to explicit read-only restrictions, and accepted or uncertain effects are not replayed. Conversation kernels retain state while alive; workflow steps start fresh kernels.
- Add comments on assistant messages, documents, table ranges, file changes, images, and PDFs, with explicit HTML-preview captures. Comments retain their exact source revision. Review and include them in a chat message or send them to a chosen conversation or task revision. Agents can reply to and resolve individual comments; sending does not resolve them.
- Add ElevenLabs synthesis and transcription, per-agent speech settings, document narration, and narrative podcasts. Supported OpenAI and ElevenLabs routes can stream audio before synthesis finishes. Conversation mode supports live transcription and interruption. Podcasts offer source-based Shorter, Default, and Longer estimates. Optional message-listening adaptation is disabled by default.
- Add usage accounting for model and media requests, with token and native-unit totals, immutable price snapshots, and separate provider-reported usage and estimates. Configured prices take precedence over the bundled catalog. Unknown usage and incomplete pricing remain visible. History cleanup is disabled by default.
- Add a Usage dashboard and read-only `query_usage` tool for costs, tokens, performance, companion services, forecasts, and stored provider limits. Compare work by conversation, task, schedule, agent, provider, model, or API client. Chat inspectors show conversation and child-work usage. Cost views keep currencies, pricing coverage, and known priced subtotals distinct. The default-enabled Usage analytics switch disables collection, processing, and analytics access after a controller restart without deleting history.
- Add retained request and proxy-session explorers with status, retries, timings, token totals, and estimated cost. Filter API-client traffic separately from Cognis inference and inspect authorized client, key, and allowlisted HTTP metadata without recording request content.
### Changed

- Open deliverables and project dashboards in separate desktop windows, including native macOS windows. The macOS companion adds app links, a configurable global panel shortcut, display selection for avatars, and Save dialogs for downloads. Spotlight restores account-local search history and uses Control-Shift-1 through 9 for numbered matches.
- New public artifact links and public deliverable links open the latest live document revision. Existing legacy artifact links remain pinned to their original revision. Comments remain attached to the revision that was reviewed.
- List assigned skills in agent context and discover other visible skills through skill_search. Skills created, imported, or edited by an agent are attached to that agent. Shared agents and system specialists can load authorized skills without gaining unrelated tools. Required asset preparation can recover on the same executor and fails before dependent output is rendered if preparation remains incomplete.

  Add a bundled Claude Code skill for scoped CLI delegation. Claude's native tools are not individually evaluated by Cognis or Intaris; use explicit permissions and an isolated executor.
- Task outcomes support direct delivery, agent handling, and optional finish instructions. Default handling sends successful results directly and uses an agent for other outcomes. Existing always-agent settings remain available as Agent. Primary agents can inspect and revise their owner's tasks from the owner's main chat. Task history distinguishes previous outputs from a replacement in progress.
### Fixed

- Preserve message order, interrupted replies, queued input, and the current request across refresh, compaction, and reconnects. Repair disconnected device-history windows and duplicate cached timeline identities. Add Force refresh to rebuild server history without discarding unsent or recoverable input. Recover PWA sessions after temporary browser-storage failures.
- Added document-local Find with Command-F / Ctrl-F, literal matching, match counts and previous/next navigation for opened deliverables and text, HTML and PDF artifacts. Chat and documents share compact search controls inside their existing toolbar, without covering or shifting content. On phones search replaces the header contents at the same height. Document action bars and extended menus offer search on touch devices. Search stays inside the document instead of reaching the Library or a view behind a modal.
- Unify document controls and revision selection across chat, Work, Library, and standalone viewers. Include assistant attachments in Work, group image revisions in a gallery, and prepare authorized documents with chat history. Improve PDF zoom, mobile controls, rich-document first paint, and public audio and video previews.
- Keep tasks queued through executor outages and recover delegated work across controller interruptions without replacing its execution identity. Preserve proven non-delivery and durable tool receipts. Agent steps can inspect uncertain mutation outcomes before continuing; deterministic steps still require explicit reconciliation rather than automatic replay.
- Fix reference-image edits, configured image-provider routing, and Gemini image retries. Recover supported inline Gemini video results without submitting generation again. Video job details can expose bounded, redacted provider rejection information only after explicit owner opt-in in a private conversation.
- Keep shared-conversation attention reader-specific, update project dashboards as work changes, and make questions and approvals easier to read without covering chat. Improve mobile navigation, Mermaid controls, history reading position, and comment-source navigation.
- Report expired or revoked Claude and ChatGPT sign-ins as requiring reauthorization. Stop affected requests instead of retrying them, show the provider and recovery action in chat and on the dashboard, and notify the provider owner or the administrator of a shared provider.
- Reduce repeated runtime, catalog, skill, and document lookups while retaining current authorization checks. Refresh executor eligibility during long-running work, improve bridge admission and reconnection, and keep updates flowing across controller replicas. Optional memory recall now defaults to a 500 ms budget; configured overrides and core memory bootstrap are unchanged.
- Allow the `manage_schedules` tool to set the auto (Default) completion delivery mode it already advertises, instead of rejecting it as invalid.
- Prevent SQLite cancellation cleanup from stalling controller shutdown by avoiding a driver release with non-idempotent connection cleanup.
- Finished tasks now link back to their task view from chat. Successful direct
  results keep their normal presentation; new failures and cancellations show a
  compact outcome card with partial output and expandable steps. Agent-handled
  outcomes use a task handling row without sending that boundary to external channels.
- Keep retained request pages and conversation usage summaries within their query budgets while preserving child-work access checks and incomplete-pricing indicators.
### Security

- Update DOMPurify to 3.4.17 and the locked devalue dependency to 5.9.4. Update Sharp to 0.35.5 to fix SVG decoding in developer image-review tools. Exclude local validation credentials and browser-review artifacts from controller image build contexts.
- Use authorized encrypted credential references for private MCP bearer authentication and executor tokens. MCP bearer resolution fails closed unless the executor connection supports credential-binding enforcement; upgrade and reconnect older executors. Isolate speech caches by owner and acting scope, recheck document access before signing audio, and reject cross-user transcription.

  Guardrails can match interpreter-run script files against executor-observed SHA-256 evidence. The evidence grants no permission by itself.
- Require the selected executor's explicit host-execution opt-in for native Python tools and evaluate nested local-tool arguments through the normal guardrail boundary. Python is not a sandbox: it can access the executor user's files, credentials, mounts, network, and processes. Enable it only on an appropriate execution host.
### Removed

- Remove automatic task workflow selection and its agent settings. Tasks without an explicit workflow use General task; explicit workflows and decomposed skills remain supported.
### Upgrade notes

- Upgrade Intaris before Cognis to a version that persists and acknowledges analytics_enabled and guardrails_enabled session flags. Cognis rejects session admission if those acknowledgements are missing or do not match.
- Upgrade controllers and executors together. Configure ingress to allow the intended upload and download sizes without whole-request buffering, and use shared artifact storage across controllers. S3 cleanup needs multipart-abort and ListBucketMultipartUploads permissions; retain an incomplete-multipart lifecycle rule. Deterministic attachment templates no longer receive inline content_b64, transfer capabilities, or signed URLs beside artifact IDs. Use artifact IDs and authorized artifact value references; URL-only, lazy, and deliverable references remain supported.
- Upgrade WebSocket executors with controllers and preserve their data and receipt directories. Unacknowledged receipts are not evicted and can block admission. Acknowledged payloads remain for up to fourteen days but may be reclaimed earlier under pressure; permanent identities still prevent replay. The default payload budget is 1 GiB. Set COGNIS_EXECUTOR_RECEIPT_MAX_BYTES and COGNIS_EXECUTOR_RECEIPT_MAX_CALLS in the executor environment to change limits. For existing systemd units, set OOMPolicy=continue and reload the manager; package upgrades do not replace custom units.
- Upgrade all controller workers before using relative-length podcasts. Apply migration 175 or the equivalent startup bootstrap. Finish or cancel these jobs before downgrading, because rollback removes their word-budget metadata. Existing jobs and explicit-minute API requests keep their previous budgets. Recorded audio sent to ElevenLabs realtime transcription requires FFmpeg on the inference host.
- Update scripted General task completion calls: blocked results require a failed outcome, and completed results require verification arrays named checks_run, results, and checks_not_run, with nonempty results. Historical outputs and custom workflows are unchanged. Retired available_workflow_ids, default_workflow_id, and workflow_selection_mode settings are discarded when execution settings are saved. Configure auxiliary_generation if recaps and workflow generation must keep the chat model previously assigned to classifier; otherwise they use the default chat route.
- Upgrade all accounting workers together; do not mix old and new traffic-dimension writers. Apply migration 187 or the equivalent startup bootstrap and allow automatic history rebuilding to finish before using affected views. Client, key, and HTTP detail cannot be recovered after detail expiry; archives retain only coarse traffic and authentication dimensions. Upgrade controller, common, and executor packages together before enabling untiered tariffs.
## [0.18.3] - 2026-11-03

### Fixed

- Keep timeline reading positions stable when wheel, touch, or keyboard input interrupts pending automatic scrolling.
- Keep dashboard provider cards compact when several quota windows and reset times are displayed, without hiding reset information or reducing refresh controls.
- Keep knowledge search and document navigation responsive after opening a search result or a document deep link.
- Keep full-chat drafts synchronized with pinned and dashboard views, and align tucked avatars at custom interface font sizes.
- Render dashboard agent chat windows from their canonical agent identity so newly opened or retained windows no longer appear empty when optional metadata is absent.
- Deliver canonical event updates to conversation subscribers that connected before the first session existed, while preserving child-session isolation.
- Keep agent configuration usable when persisted model metadata omits optional reasoning-effort or fast-mode capabilities.
- Allow controller package publication to retry an existing immutable project tag without rebuilding from a newer application revision, and recognize the build-only workspace manifest needed by locked dependency export while continuing to reject executor implementation in controller images.
- Preserve known running activity during incomplete executor telemetry, including conversations without unread messages and activity reported across Waiting pages.
- Finish database-session rollback before a cancelled caller returns, including repeated cancellation during cleanup.
- Mark conversations as read only after their displayed transcript is loaded, preserving read tracking across minimized windows and avoiding duplicate updates when pinned panels resume.
## [0.18.2] - 2026-10-24

### Highlights

- **Cognis on your Mac.** Menu-bar chats, pinned conversations, native notifications, and simple tool approvals.
- **Reusable agent hooks.** Configure reminders, memory consolidation, and skill learning, and inspect hook runs in chat.
- **Continue elsewhere with `/handoff`.** Prepare a continuation brief to copy or open in a new conversation.
- **A Library for ongoing work.** Organize documents, inspect revisions, manage sharing, and favorite deliverables.
- **Make the workspace yours.** Porcelain, Classic, Gruvbox, and Paper themes with Light, Dark, and System modes.
- **Offline continuity.** Read cached PWA content, queue uploads, and switch between cached chats faster.
- **Find work with Spotlight.** Search agents, conversations, projects, tasks, and Library documents.
- **Latest recovery and chat fixes.** Concise tool-output recovery guidance, compact session headers, correct attempt ordering, and historical-window placement.

### Changed

- Chat deliverables now flow with assistant responses, using chat typography and colors without a nested document frame. Right-aligned icons provide fullscreen, download, sharing, and responsive contents navigation; less frequent actions live under the three-dot control. Revision history stays below the content. Fullscreen and standalone readers retain their publication layout.
- Move chat diagnostic downloads to the bottom of the Session tab's collapsed context and runtime details, match the session panel styling, and include the conversation identifier in the filename.
- Keep the documented incomplete regression baseline: no further unit, contract, GUI/browser, or live-model tests are rerun at the operator's request, and skipped checks are not passing qualification. Ad-hoc Mac packages remain unnotarized and use normal app-icon/text notifications.
### Fixed

- Reduce prompt-cache invalidation by reusing accepted within-turn context when only a soft sizing target is crossed, while retaining exact pressure checks, protected evidence, and durable projection decisions.
- Keep completed local admissions and session compactions that demonstrably precede a partial history window before that window, instead of trailing recent activity. Preserve canonical history, live work and cached source data.
- Unify compact task and subsession headers with consistent info controls, chronological attempt selection below the step title, and a subtle running indicator without redundant follow labels or refresh rows.
- Reduce repeated tool-output recovery guidance in context placeholders and truncated previews while preserving recovery handles, anchors, artifact references, and existing replayed context.
- Use the standard square-and-up-arrow share icon consistently across document readers and native attachment sharing, keeping it distinct from copy-link and open-in-new-window controls.
- Chat now opens reliably after sign-in or delayed device-storage initialization without requiring a detour through the dashboard.
- Restore Classic Dark's original translucent warning, success, error, and information colors in deliverables, including callouts, badges, and status treatments. Other themes now preserve semantic background and border transparency.
- Restore Classic as the default theme and its subdued dark color treatments, improve neutral Porcelain surfaces and Paper typography, and align tool, approval, queued-message and mobile overlay styling with the selected theme family.

  Explicitly accept a 405 KiB chat-critical JavaScript budget for this release's measured 402.0 KiB graph; CSS and largest-chunk limits remain unchanged.
- Recover transient inference admission failures and preserve provider-directed waits in task steps without spending workflow quality attempts. Failed turns and tasks show the actual error with a recovery action; chat recovery controls clear when execution resumes or a newer message supersedes the failure, including after reconnect.
- Task logs retain compacted session history, expose earlier retry attempts, and share the dashboard and pinned chat workspace with work inspection and parent navigation. Upward history loading advances through event-only pages without requiring another scroll gesture.
- Allow task retries after pre-execution setup failures to create their first step session when the current attempt has no execution evidence, while keeping missing previously-started context and history-dependent initialization fail-closed.
## [0.18.1] - 2026-10-24

### Highlights

- **Cognis on your Mac.** Open agent chats from the menu bar, keep pinned conversations on your desktop, and review notifications or respond to simple tool approvals without returning to the main workspace.
- **Reusable hooks for your agents.** Configure reminders, memory consolidation, and skill learning around agent activity. Customize bundled hooks in Tools and inspect their runs directly in chat.
- **Continue elsewhere with `/handoff`.** Prepare a self-contained continuation brief—even during an active turn—then copy it or open a new-conversation draft.
- **A Library for ongoing work.** Organize documents by project, inspect revisions, manage sharing, and favorite artifacts and deliverables.
- **Make the workspace yours.** Choose Porcelain, Classic, Gruvbox, or Paper, each with Light, Dark, and System modes.
- **Offline continuity.** Read cached PWA content, queue attachment uploads, and switch between cached chats faster.
- **Find your work with Spotlight.** Search agents, conversations, projects, tasks, and Library documents with Command-K or Control-K.

### Changed

- The first published 0.18 distributions bring the macOS desktop companion, reusable agent hooks, in-app handoff, the Library, refreshed themes, offline continuity, and Spotlight project/session search. This release supersedes the unpublished 0.18.0 binary candidates; post-freeze upstream changes are reserved for a following release.
- Retain the documented incomplete regression baseline. At the operator's request, further unit, contract, GUI/browser, and live-model tests are not rerun; these waivers are not passing qualification. Ad-hoc Mac distributions remain unnotarized, use normal app-icon/text notifications, and are not Developer ID releases.
### Fixed

- Find the exact reviewed macOS draft prerelease through the release-aware lookup before attaching immutable assets, retaining draft/prerelease checks and refusing asset replacement.
### Security

- Replace environment-specific presentation, executor, and workflow examples with coherent synthetic fixtures, preserving identity relationships, protocol syntax, canonical digests, and long-token layout pressure.
## [0.18.0] - 2026-10-24

### Highlights

- **Cognis on your Mac.** Open agent chats from the menu bar, keep pinned conversations on your desktop, and review notifications or respond to simple tool approvals without returning to the main workspace.
- **Reusable hooks for your agents.** Configure reminders, memory consolidation, and skill learning around agent activity. Customize bundled hooks in Tools and inspect their runs directly in chat.
- **Continue elsewhere with `/handoff`.** Prepare a self-contained continuation brief—even during an active turn—then copy it or open a new-conversation draft. Add a focus to guide what the handoff preserves.
- **A Library for ongoing work.** Organize documents by project, inspect revision history, manage sharing, and favorite useful artifacts and deliverables. Favorites follow revisions as your work evolves.
- **Make the workspace yours.** Choose Porcelain, Classic, Gruvbox, or Paper, each with Light, Dark, and System modes that carry through chat, documents, and native chrome.
- **Better continuity on unreliable connections.** Read cached PWA content offline, queue attachment uploads, and switch between cached chats faster. Recovery better preserves reading positions and live updates.
- **Find your work with Spotlight.** Use Command-K or Control-K to search agents, conversations, projects, tasks, and Library documents, or jump straight to common actions.

**Mac distribution:** Initial builds use ad-hoc signing, not Apple Developer ID signing or notarization. macOS or organizational policy may restrict installation. Notifications use the standard app icon rather than communication avatars.

### Added

- Add admin API-key controlled, bounded controller profiling with light, CPU, allocation and combined levels, HA incarnation targeting, finite boot configuration, automatic cleanup and bounded structured reports instead of verbose diagnostic logs.
- Assign reusable agent lifecycle hooks for reminders, background actions, and idle skill learning. Manage hooks in Tools, inspect their outcomes in chat, and run detached prompts in internal child sessions without adding separate user notifications.
- Added account-aware light, dark and system appearance across application surfaces, chat, tools, documents and settings, with semantic colors, themed prose and syntax, live browser/native chrome updates, and system-matched loading, recovery and Apple launch screens. Embedded rich documents now follow application appearance by default while preserving explicit viewer Light, Dark and operating-system System choices. Standalone image viewers have a safe-area fallback, and printed documents hide viewer controls.
- Cognis now includes Porcelain, Classic, Gruvbox and Paper appearance families, each with independent Light, Dark and System modes. Classic is the default, Gruvbox restores its original warm yellow Light surfaces, and Paper provides warm neutral styling with compact chat text. Embedded readers retain the selected family when overriding their mode, and native chrome updates when the family changes.
- Export metadata-only chat diagnostics from the conversation inspector to investigate device-specific history ordering without sharing message text or tool inputs and outputs.
- Command-F and Ctrl-F focus and select the current view's existing search field. Full conversation search opens when collapsed; dialogs keep search focus inside their own content.
- Add disabled-by-default, pod-selectable controller resource attribution with delayed bounded CPU stack sampling, content-free session memory estimates, and automatic cleanup.
- Star artifacts and deliverables from chat, tasks, previews, or Library and filter Library to personal favorites. Favorites follow document revisions without granting access.
- Add `/handoff [focus]` to prepare a self-contained continuation brief with the current model and effective context, including during an active turn. Handoffs persist in the source session and appear as collapsed cards with copy and new-conversation draft actions.
- Include read-only allocator and Python-block accounting in explicitly requested resource captures to distinguish live allocation pressure from retained free arena space.
- Use an authenticated OpenAI-compatible inference API and reusable Cognis routes with operation-aware targets, fallback and recovery policies, executor selection, and provenance-backed model capacity information.
- Agents can manage model providers, custom inference routes, task model bindings, typed system settings, workflow profiles, and executors through dedicated action tools and bundled management skills. QuickStart enables full instance-management access for the first agent by default, with explicit opt-out and existing authorization, policy evaluation, and approval checks preserved.
- Browse the Library, author versioned artifacts, revise deliverables, and inspect revision history. Organize documents by project and manage public links and explicit agent access from the shared document viewer.
- Add an explicit public ad-hoc Mac distribution class with sealed bundles, truthful trust metadata, dual-architecture verification and post-publication Homebrew cask generation, while retaining strict Developer ID and notarization requirements for trusted releases.
- Add macOS companion packaging with whole-bundle development signing, gated Developer ID and notarized DMG builds for Apple Silicon and Intel, and checksum-verified Homebrew cask generation. Developer ID and notarized distribution remain gated on signing credentials and release qualification.
- Configure Claude prompt-cache lifetimes per provider model: primary conversations default to one hour; task steps, delegates, compaction, hooks and background requests default to five minutes. Supported TTL choices follow the backend. Route sessions may probe a higher-priority target after the shorter of their cache lifetime and the new one-hour idle-failback timeout, without interrupting active requests.
- The macOS companion now lists available agents independently of pins, provides native local Settings, aligns desktop avatars with the web rail, and improves conversation chrome and notification presentation. Simple tool approvals support unlock-required actions through the existing authenticated client; communication avatars remain subject to macOS signing capability.
- Review a persistent notification inbox and take targeted actions on approvals, task failures, and provider issues. Authentication challenges expire with their notifications, and project task results can notify relevant collaborators.
- Add bounded, yielding session-field memory estimates to explicitly requested diagnostic captures, without global heap scanning, tracing or cache mutation.
- Collaborate through project members, project agents, shared knowledgebases, and read-only shared conversation history. Discover accessible project chats and browse paginated project tasks, schedules, conversations, and documents.
- Monitor provider health and usage from the dashboard, configure SystemOne inference, and use GPT-6.1 Sol, Claude Sonnet 5.5, and Claude Haiku 5.5 with model-aware reasoning controls.
- Browse release history and compare versions in the app's What's new view. Development-time changelog fragments now generate the same release material for the app and GitHub release notes.
- Projects navigation returns to the last opened project and tab on this browser, with an explicit All projects link and a fallback when the project is no longer accessible.
- Global Spotlight-style command palette with keyboard navigation, bounded metadata search, schedule and settings discovery, dashboard-aware opening, and document workspace windows.
- Dashboard workspace windows can display artifacts and deliverables using the Library document reader.
- Set up Cognis in three guided steps: configure system models with an OpenRouter preset or custom providers, connect an executor, and create an assistant. Optional Codex, OpenAI, and Anthropic accounts, provider and model pickers, reusable agent templates, and avatar generation are available without exposing advanced configuration by default. Setup completion is saved across devices.
### Changed

- Manage system-agent overrides, secondary agents, owner-scoped credentials, and primary executors through agent management tools. Switch conversation profiles quickly and inspect acknowledged guardrail policy in agent context.
- Use transparent vector Cognis artwork across the web app, favicons, installation icons, splash screens, and notification assets.
- Bring compact chats closer to the main chat experience, improve pinned-agent dock interactions and display controls, and show activity-ordered projects on the dashboard. Preserve upload progress, improve command navigation, and apply selected chat motion settings.
- Dashboard provider columns now stack up to four health-only providers and fill space below short usage reports without reducing text size or hiding controls.
- Local Compose now uses the published Mnemory and Intaris images from GHCR, matching the Cognis and executor image references.
- Download portable HTML and Markdown deliverables with consistent hosted and offline rendering, preserved links, image previews, and simpler gallery controls.
- Cache the PWA workspace for offline reading, queue attachment uploads, and recover more efficiently on slow or interrupted connections. Keep previews and file saving inside the installed app, with faster cached chat switching and smaller reconnect payloads.
- Load the optional session details view on demand, with an explicit retry if its code cannot be loaded.
- Prepare portable skill assets and shared Python environments from the current load manifest, ingest knowledgebase images, and move running shell commands to the background from chat.
- Make Spotlight a balanced launcher for work, app pages, main-agent conversations and project dashboards. Keep idle results stable, use conversation avatars as primary icons, and pin desktop search above Notifications.
- Keep Spotlight stable during background updates, allow slower bounded searches, show agent avatars and update times, distinguish file producers, prefer agent-produced files, show only the latest scheduled task execution, and add subtle multi-select result-type filtering.
- Reduce repeated agent metadata reads and coalesce concurrent dashboard and sidebar requests without retaining authorization-sensitive results between requests.
### Fixed

- Retry executor bridge capacity rejection within the original tool admission, with bounded attempts and accurate capacity errors. Do not repeat duplicate admission requests or replay potentially started commands.
- Image tools and artifact read tools can use current agent avatars when the acting user has use or manage access to the owning agent.
- Agent settings now confirm hook and delegation changes with success notifications instead of repeated save notices. The detail header includes the agent avatar, and memory and guardrail settings are grouped with advanced evaluation controls disclosed separately.
- Load custom-route capabilities for existing agent and profile selections on every editor tab. Distinguish loading and discovery failures from unsupported thinking efforts, preserve saved settings, and offer retry before saving.
- Preserve upstream rate-limit and outage categories when every inference-route target is blocked. Report the earliest eligible target's retry time and retry known resets within the request deadline without resetting attempt or fallback limits.
- Forward exact approval receipts on evaluated retries, reconcile restored escalation pauses before claiming work, contain evaluation conflicts, and report sanitized evaluation-size and response errors.
- Artifact recovery recognizes URLs from the configured public controller origin instead of assuming a deployment-specific host. Set `COGNIS_PUBLIC_BASE_URL` when exposing the controller.
- Direct chat now exposes configured core tool groups by default under the direct soft profile, while preserving other profile, hard-profile, and explicit-exclusion limits. When a provider rejects a named tool omitted from the request—during stream opening or generation—Cognis can promote an exact authorized match or provide bounded discovery guidance and retry with a changed request. Recovery limits persist across same-turn reentry and reset for a new turn.
- Prevent background delegates from failing when the spawning chat turn completes and releases its execution fence.
- Upgrade databases that were initialized by startup bootstrap without duplicate-table failures in project knowledgebase assignments, inference routes, and agent hooks. Migrations preserve existing tables and create any missing tables independently.
- Prevent large rendered-history snapshots from failing workflow steps with Intaris HTTP 422 errors by recording size-bounded chunks and replaying only complete snapshots without truncating context.
- Preserve projection decisions across cached retries, keep per-call executor schemas stable during temporary outages, and retain native Responses message envelopes across cold replay and profile changes to avoid unnecessary prompt-cache misses.
- Keep background chat preloading from replacing the selected conversation, interrupting its loading, or changing its reading position. Clear stale compact-chat command results and isolate pending controls when switching conversations or session views.
- Foreground chat opening now handles account-context invalidation during cached hydration with the existing session-verification state, while ignoring obsolete route failures and ordinary navigation cancellation.
- Keep live sidebar updates ahead of device cache, preserve timeline refreshes received during loading, and show cached-history freshness. Compact and pinned work summaries avoid full history reads; reconnects refresh promptly without bypassing rate limits. Add bounded send/admission timing diagnostics.
- Keep chat history reading positions stable when another view loads older messages, preserve live updates during canonical history recovery, and allow upward history loading to complete when animation frames are paused.
- Prevent Safari scroll jumps and oversized chat rows, stabilize jump navigation and Markdown streaming, restore project document previews, and order grouped Library folders by document recency.
- Assistant messages use the same 70-character reading measure as rich deliverables in every theme; in Paper, tables and code blocks can use the full chat width. Long unbreakable text in notices no longer widens the chat or adds a horizontal scrollbar.
- Prevent older completed messages and tool activity from reappearing below recent chat history after reconnecting or reopening a PWA during a long-running turn.
- Restore Classic Dark to its pre-theme colors: original translucent status tints, slate-950 sheets, finer muted/strong text and raised-surface shades, and original timeline label tints. Make Paper status and category colors distinguishable.
- Avoid rebuilding the complete tool registry for individual classified-tool lookups and ledger updates, while retaining effective-definition validation and current handler routing.
- Prevent multi-minute context preparation delays in long conversations by avoiding repeated scans of tool groups during prompt pruning.
- Reuse exact native token counts for unchanged history blocks during long turns, reducing repeated context-projection CPU work without retaining message contents or changing prompt budgets.
- Reduce temporary memory used by large executor bridge results while preserving canonical bytes, and make the optional startup allocation profiler honor the profiling flag and selected pod.
- Refresh executor inventory and diagnostics, bind controller-owned readers correctly, return explicit errors for unsupported executor RPC methods, and avoid downloading tokenizer or model metadata assets at runtime.
- Synchronize admitted user messages across open chat clients through canonical append notifications and cursor recovery. Preserve timeline state across equivalent scope updates and isolate conflicting API providers without resetting sibling views.
- Clicking the background on main and project dashboards now minimizes all workspace windows without activating background controls, matching the tray's minimize-all action.
  New chats reuse the previous eligible agent across dashboards and Chat. Sending a message resumes timeline follow, typing in compact composers preserves the viewport, and composer profile settings are hidden during active turns. Chat defaults its channel filter to Web while retaining explicit saved selections.
- Restore existing dashboard conversation windows without requiring a free retained-window slot or redundant allocation metadata request. Distinguish visible, retained, and pinned-agent limits from owner, synchronization, and other open failures.
- Dashboard windows update their position and size once per animation frame while dragging or resizing, and save the final geometry when the gesture ends instead of writing saved layouts on every pointer movement.
- Deliverables inside Cognis now follow your account appearance, including task outputs and fullscreen views. Separate light, dark, and system controls apply only to external document readers.
- Projected conversation history now replays consistently across turns and cold-cache recovery without storing duplicate prompt or tool-result content.
- Controller and executor Docker builds now install runtime dependencies using frozen constraints exported from the workspace lockfile, preventing incompatible dependency drift.
- Resume persisted workflow gates after controller recovery without rerunning completed steps or clearing their evidence. Preserve original deadlines, arbitrate timeout/operator decisions durably, clear pauses atomically on cancellation, and reject stale gate responses from refreshed task controls.
- Prevent database deadlocks between trusted memory evidence or conversation forks and TODO persistence, and keep database pool occupancy metrics accurate after failed connection acquisition or invalidation.
- Executor image builds prefer IPv4 and retry when downloading browser components, so transient Playwright CDN failures no longer block executor releases.
- Release consumed executor WebSocket payloads and dispatch closures before waiting for the next frame, rather than retaining completed results on idle connections.
- Executor container images install the shared common package normally instead of as an editable workspace link, fixing executors that crashed at startup with a missing `cognis.json_codec` module.
- Preserve executor assignment notices in canonical session history across turns and cache loss, without duplicating unchanged notices or rewriting earlier assignments.
- Fix repeated Intaris rejections of executor recovery notices by deriving valid, stable append keys from persisted notice identities. Pending notices retry without changing their content or already-valid append keys.
- New conversation turns no longer inherit an expired executor recovery deadline from previous work. Executor recovery waits and successful recovery now appear as chat notices, and recovery timeouts explain how to continue.
- Preserve original message times in newly forked history, show completed delegations correctly after reload, render delegation results as Markdown, and confirm message-level forks before creating a separate conversation.
- Gemini image generation and editing now translate requested sizes into image configuration on both controller and executor instead of sending an invalid image_size field.
- Gemini providers configured with a base URL now use the versioned inference endpoint correctly, including custom provider roots.
- Gemini model discovery now includes Veo video-generation models and marks them as video models instead of chat models.
- Show handoff progress and failures immediately, update the result live, and preserve it across history loading. Simplify recipient creation to a source-defaulted profile picker, prepare a normal unsent draft, and persist a link to the created conversation on the original handoff card.
- Allow compatible Anthropic account fallback when earlier completed history contains Responses tool metadata. Keep active Responses continuations blocked and preserve explicit incompatibility errors instead of reporting false integrity failures.
- Keep configured hook assignments distinct from rendered conversation evidence when creating child guardrail sessions. Hook prompts explicitly request execution of the configured task while retaining inherited restrictions.
- Run each detached hook once within its original configured timeout, using normal session recovery instead of hook-level retries. Queue later eligible occurrences, retire legacy retrying runs as failed, and keep failed delivery independent of execution. Web chat now uses friendly hook names and one live hook card with direct session inspection.
- Resume unfinished hook sessions safely after runtime metadata or profile changes. Persistent failures now retry automatically with capped backoff and visible failure details instead of permanently blocking later occurrences; previously blocked runs recover without manual resets.
- Prevent historical idle-hook backfill, deduplicate and bound evidence reads, wait for the configured idle boundary, and prevent learning hooks from modifying system skills.
- Idle hooks now evaluate work accumulated since that hook's last successful detached run in the conversation, so a question-only follow-up no longer hides earlier work. Failed runs retain the unprocessed work window.
- Keep image attachments and composer text recoverable when local storage fails, and serialize reactive attachment metadata safely before queuing messages in full and compact chat.
- Image tools now offer provider-qualified models from active, visible providers and resolve those references consistently with model routing, including publisher-namespaced model IDs.
- Avoid restarting pending metadata reads for routine executor, progress and notification updates, keep sidebar refreshes serialized, and stop unrelated executor and schedule changes from rebuilding open chat timelines. Access-change and account fences remain immediate.
- Preserve numeric output-token counts in inference logs, accept optional operation and request correlation metadata without forwarding it to providers, and add bounded route labels to inference metrics. Accounting now distinguishes unknown usage from reported zero and emits nullable price estimates with provenance rather than implying failed requests are free.
- Delegated sessions inherit the parent's Intaris policy, including nested mode changes, instead of selecting the child agent's default. New agent and policy-editor defaults are Balanced; explicit Legacy selections remain available. Policy refreshes return the canonical acknowledged policy, and stale-approval messages no longer assume that a session change was the cause.
- Load the new-chat dialog on demand instead of including it in every conversation's initial JavaScript bundle.
- Library and chat share artifact previews for Markdown, CSV tables, Mermaid diagrams, highlighted code, PDF, HTML, and media. Markdown renders immediately on opening; editing preserves the original source with an optional draft preview.
- Calm the light themes: neutral status and category containers without colored outlines, a single-tone tool timeline, borderless assistant messages with soft user bubbles, and brighter Classic and Porcelain content surfaces with darker navigation.
- Rework the light themes: near-white content on a faintly tinted frame, hairline borders, and sentence-case section labels instead of letter-spaced uppercase.
- Keep the loading screen visible while restoring a session on the login page, avoiding a brief sign-in form flash for signed-in users.
- Restore LSP metrics for configured executors whose display names are not valid metric labels. Use a validated configured executor ID as the fallback, retain collision and cardinality guards, and use the same label in executor inventory.
- Ad-hoc macOS builds no longer include a restricted communication-notification entitlement that prevents launch. Notifications retain agent names, text and actions using the standard app icon.
- Preserve the pinned-agent rail's avatar glow at its scroll boundaries, including the first and last avatars, without removing activity or selection effects.
- Close MCP transports in their owning task when initialization is cancelled or times out, preventing orphaned background tasks and sustained executor CPU usage.
- Click outside a modal to dismiss it across the app. Unsaved editor and creation drafts ignore accidental backdrop clicks, including artifact edits and mobile sheets.
- Preserve native tool-call argument formatting across executor and controller replay, expose bounded authorized tools before bundled hook inference, and log content-free projection trigger and budget diagnostics.
- Restore isolated HTML artifact previews in the macOS app, including inline scripts, styles and supported embedded media. Clear previews on account changes and reject native actions initiated by artifact frames.
- Schedules no longer wait for conversation history or creation-only options before appearing. Task, schedule, agent, and workflow pages show primary data independently of auxiliary configuration lists, and avoid redundant list requests.
- Preserve retained results in partially pruned parallel tool batches across normal cycles and cold replay, protect interruption evidence, and retain pending projection decisions during below-threshold retries.
- Pinned avatar conversations now stay open in the web app and PWA until dismissed or navigation changes, instead of closing immediately after selection.
- Project instructions loaded mid-turn retain their canonical role across live requests and cold replays, preserving Anthropic prompt-prefix continuity.
- Normalize Anthropic artifact images and high-resolution JPEG/MPO attachments before inference, preserve supported thinking transitions and tool replay, and keep retry ownership and provider-qualified model identities consistent.
- Controller-only annotations are now removed from provider-bound messages across chat, Responses, streaming, and executor-routed inference, without modifying nested provider payloads.
- Prevent internal question JSON from appearing as an assistant response after answering structured questions, including when replaying existing chat history.
- Clear submitted chat messages from the composer reliably when draft autosave overlaps durable queue storage, while preserving newer edits and drafts from other windows.
- Continue from saved work when native reasoning output times out instead of ending the turn after one attempt, and show one error card for model and turn failures belonging to the same turn.
- Python distribution metadata now names the existing Business Source License, and controller source archives exclude private qualification reports.
- Align runtime test fixtures and UI contract checks with current session, document-access, sharing, and collaboration behavior. Correct controller type annotations and fail cleanly when a manual hook has lost its foreground session or artifact storage is unavailable. Release qualification now records risk-based coverage and infrastructure gaps explicitly.
- Preserve prompt-cache reuse by keeping controller tool discovery append-only through call_tool, replaying newly recorded native tool batches faithfully across turns, and avoiding larger omission placeholders for short tool outputs.
- Reduce repeated artifact permission queries, executor discovery queries, and dashboard inventory transfers. Restore PostgreSQL inventory metrics, release metadata connections before external requests, and add snapshot-copy and database connection diagnostics.
- Preserve provider failures when a Responses stream ends after an error event, and keep the original workflow failure instead of replacing it with a missing-deliverable error.
- Interrupted detached hooks now resume their existing session after reconciling outstanding tools. Bounded retries retain the same-hook reservation, expose retry/blocked card states, and allow manual continuation without starting duplicate work.
- Keep conversation-list and child-work refresh signals during WebSocket stream pressure, and prevent continuous child updates from repeatedly restarting sidebar requests. Coalesce follow-up reads without polling; reconnect saturated connections instead of leaving views silently stale.
- Reduce controller memory pressure from S3 retention inventories, avoid decoding unselected executor tool schemas for explicit assignments, and reuse the optimized JSON decoder for database reads without changing stored JSON.
- Stop recording and replaying full-transcript prompt snapshots that can exceed event limits and fail turns, while retaining threshold-triggered context projection and independent cache fixes.
- Reconcile recovered-turn notices with canonical history instead of changing payloads under an existing idempotency key. Report event-store failures separately from model failures. Refresh session details after model switches, preserve actual serving-provider provenance, and prevent stale context or quota data from replacing newer runtime measurements.
- Resolve thinking-effort and fast-mode options from custom route target models in agent and runtime-profile settings, offering only options supported by every fallback target.
- Preserve request-scoped routing notices across turns and cold-cache replay, and keep the executor-switch tool schema stable during temporary executor outages without relaxing runtime target validation.
- Preserve task intention across compaction, split oversized single turns, and bound consecutive compaction recovery without blocking progress. Defer automatic recaps until attached background work finishes.
- Preserve Unicode text and formatting when copying selected text from standalone deliverables, shared previews, and offline HTML, avoiding native clipboard URL encoding of colon-prefixed prose on iPadOS.
- Conversations without an active session now render correctly before a runtime model selection is available.
- Settings loads only the selected section instead of waiting for unrelated diagnostics and registries. Opening or switching settings sections no longer triggers an unsaved-changes warning without edits; real unsaved edits remain protected.
- Keep conversation drafts synchronized across full chat, dashboard, and pinned views so an untouched composer cannot block attachment sending. Remove dashboard chat refresh layout jumps and prevent empty search results from flashing during the search debounce.
- Conversation sidebar rows no longer shift when opening a conversation reveals its bookmark button.
- Recover stale conversations, unread indicators, and child-work activity when reopening the conversation list without polling. Keep current child TODOs and status authoritative over cached delegation previews.
- Sidebar and Waiting updates now recover automatically after temporary failures, with bounded retries that pause while the app is hidden or offline. Resource-local failures no longer unnecessarily delay independent refreshes; server-directed cooldowns remain shared.
- Restore single-click conversation forking without a browser confirmation dialog, with immediate progress feedback and duplicate-click protection.
- Keep skill loading and tool discovery append-only: activation no longer changes direct tool schemas, dynamic-skill sessions use a stable discovery surface, and canonical replay preserves skill instructions, activation eligibility, attachment context, and artifact reminders.
- Add a touch-accessible Spotlight magnifier beside Close in the mobile navigation menu, with safe focus handoff and tap-outside dismissal.
- Show configured project avatars or colored project icons in Spotlight instead of a generic project symbol.
- Finish stale approval waits immediately when Intaris invalidates their session or policy receipt, including after controller restart. Return a re-evaluation result instead of recording a human denial, and remove the settled approval card without bypassing freshness checks or executing the tool.
- The loading screen and browser color now use the last selected theme on this device instead of Classic, and Library and attachment preview controls, the image viewer, and model cache settings follow the active theme.
- Remove favorite stars from chat attachment rows and image thumbnails. Favorites remain available in previews and Library with smaller, borderless controls.
- Restore Classic as the default theme and its subdued dark color treatments, improve neutral Porcelain surfaces and Paper typography, and align tool, approval, queued-message and mobile overlay styling with the selected theme family.

  Explicitly accept a 401 KiB chat-critical JavaScript budget for this release's measured 400.8 KiB graph; CSS and largest-chunk limits remain unchanged.
- Keep accepted tool history stable across turns and reduce old tool evidence only above the lightweight-compaction threshold, preserving provider cache reuse.
- Settle open chat timelines when the account or server context is invalidated, release obsolete subscriptions, and offer verified same-account recovery instead of leaving a loading spinner. Bound typed JSON requests through coordination and response decoding, and cancel pending-request reads when their view closes or changes.
- Share cached timeline data and in-flight loads across full chat, compact chat, dashboard, and tray views. Bound stalled response-body reads, keep critical history errors actionable, and advance empty history without depending on animation frames. Load the optional Work inspector on demand to reduce chat startup resources.
- Resolve unambiguous MCP server-name references consistently across skill loading and tool discovery/dispatch. Report unresolved skill bindings explicitly and return exact tool matches before relevance-ranked results, including already-visible tools.
- Avoid repeating successful audits of unchanged native tool contracts during registry construction, while preserving fresh validation for modified contracts, arguments and authorization.
- Pinned and native tray chats load account-owned chat preferences on cold open and refresh changes made in other windows. Expanded activity groups use consistent compact spacing between tools and assistant messages.
- Preserve controller reminders and mid-turn context notices in their original prompt positions across turns, allowing provider caches to reuse conversation history.
- Wait for TypeScript project diagnostics on the first semantic query so cross-file definition lookup resolves imported symbols instead of stopping at a temporary single-file import alias.
- Preserve existing video job tables when applying database migrations after startup bootstrap.
- Fix Codex turn failures caused by unsupported regular expressions in video-tool cost schemas, while preserving exact decimal cost validation and spending limits.
- Veo routing now accepts the video-generation metadata returned by model catalogs. Video spending limits are optional and off by default, with advanced account controls instead of a JSON and job-administration panel on Routing.
- Prompts now require models to use only tools visible in the current request and discover other authorized capabilities before calling them. Deterministic provider rejections for unavailable tool names stop retrying the identical request, while transient failures retain recovery.
- Fixed workflow steps failing with an async database error when an executor briefly disconnects between steps. New step sessions retain the executor pin generation, and recovery safely reloads pin identity after a rolled-back projection.
- Preserve scheduler wakeups received during an evaluation pass, reduce direct-turn discovery queries, and prevent older settled failures from hiding pending failure notifications.
### Security

- Enforce explicit agent artifact entitlements, project membership, revision visibility, and document-sharing boundaries. Keep published rich-artifact references scoped to their authorized source and retain authenticated owner-only recovery for expired deliverable links.
- Keep managed-session deliverables scoped to their authorized conversation when viewing or downloading them in the browser. Owner authentication alone no longer provides an unscoped fallback around managed-conversation access checks.
- Reconstruct example fixtures with fictional data and remove operational screenshots from the current documentation.
### Upgrade notes

- Do not mix hook-enabled and older controller runtimes that cannot honor hook activation and handoff boundaries. Follow the Agent Hooks rollout guidance, preserve durable hook state, and reconcile active work before a downgrade.
- Upgrade Intaris alongside Cognis before enabling the new plan-mode and workflow-step policy behavior. Plan mode does not itself impose a strict read-only ceiling; select the required outcome bounds explicitly. Older Intaris versions can still infer a read-only ceiling from plan mode. See the Chat modes guide.
- Back up the Cognis database and apply migrations before starting the new controller fleet. This release adds project collaboration, document revisions/access, notification inbox, inference routes, and hook persistence. Upgrade executors as well so skill asset preparation and background shell controls use matching contracts.
- Video policy `enabled` now enables optional spending limits rather than enabling video generation. Missing or disabled policies no longer block authorized video requests. Existing enabled policies retain their limits, and explicit request cost caps still require a model estimate. Provider charges and normal tool authorization continue to apply.
### Release qualification

- The full final-tree regression baseline is incomplete. Existing qualification and focused checks for the latest fixes are retained; expensive broad-suite reruns were explicitly waived.
- GUI/browser acceptance, including headless Linux and native macOS scenarios, was explicitly skipped and is not a passing qualification. Earlier GUI failures have not been cleared by that waiver.
- Initial Mac artifacts are ad-hoc signed and unnotarized. Dual-architecture packaging does not establish physical Intel runtime or communication-avatar acceptance.

## [0.17.1] - 2026-09-28

### Fixed

- Made automatic continuation replay stable across server-generated message timestamps while retaining conflicts for changed explicit metadata, content, and metadata origin.
- Preserved exact replay of existing pending and completed admissions without rewriting persisted hashes or inferring the origin of legacy metadata. Legacy timestamp differences remain conflicts.
- Replaced full-history window ranking in recent Work activity with batched index-ordered pages, preserving distinct top-ten results and artifact visibility checks.

### Upgrade notes

- Coordinate the upgrade of all controller admission writers, including automatic continuations and recovery workers. Quiesce ingress and finish active admission/handoff transactions before stopping old writers; resume only after the complete fleet is upgraded.
- Mixed-version admission writers and rolling downgrade after new admissions are not supported. Keep existing rows and idempotency keys; reconcile uncertain requests rather than deleting records or issuing fresh keys.
- Existing requests without their original timestamp cannot use the exact legacy replay path. Draining pending work alone does not remove this limitation.
- This patch supersedes the tagged v0.17.0 candidate. Do not replace its tags or attach patch-version binaries to that tag.

## [0.17.0] - 2026-09-28

### Added

- Added persistent conversation recaps with recent-work summaries and richer artifact and file-diff statistics.
- Added configurable agent outcome bounds and conversation YOLO mode, with stored-policy verification and visible guardrail warnings.
- Added GPT-6 Sol and Luna and Claude Opus 5.5 model support, plus a credential-free model-support preflight for repository maintainers.
- Added agent and task escalation timeout overrides and richer, routed PWA notifications.

### Changed

- Improved context budgeting with calibrated projection, deferred compaction, and durable lifecycle cards.
- Improved deliverable diagrams, tables, typography, and print layouts; reduced cache and runtime memory pressure.

### Fixed

- Fixed turn recovery, retry lineage, managed-session ownership, and deterministic workflow keys across sessions.
- Fixed Anthropic orphan results, executor bridge cancellation, and transient Chat recovery notices.
- Fixed documentation navigation, recap projection typing, and evidence-admission validation at the release gates.
- Fixed mobile rich-text overflow and Mermaid layout with reduced motion enabled.
- Preserved authoritative streaming updates without Redis and restored table-of-contents focus across responsive layout changes.
- Prevented competing service-worker registrations from reinstalling the same worker during page reloads.

## [0.16.0] - 2026-09-20

### Added

- Added attributed application observability, event-loop lag monitoring, maintenance leases, and high-availability background-worker diagnostics.
- Added richer publication-grade deliverables with reusable viewer chrome, embedded fonts, additional composed scenarios, and expanded responsive visual coverage.
- Added broader artifact preview support and managed delegation policy controls across the API, runtime, and agent editor.

### Changed

- Improved canonical history replay, context assembly, compaction publication, session caching, and turn scheduling for large or interrupted conversations.
- Improved Chat v2 runtime authority, sidebar activity reconciliation, attachment presentation, and mobile/PWA layout behavior.
- Improved deliverable export, retention, maintenance, and validation contracts.

### Fixed

- Fixed transient history gaps, claimed-turn reconciliation, abandoned stream progress, stale runtime state, and recoverable tool-result ordering.
- Fixed Anthropic tool adjacency and orphan-result handling across interrupted or retried turns.
- Fixed high-availability maintenance fencing, executor connection ownership, session-fork recovery, and trusted-evidence queue behavior.

## [0.15.0] - 2026-09-13

### Added

- Added bounded LSP code intelligence with capability negotiation, compact query output, directory outlines, call hierarchy, edit diagnostics, telemetry, and real-server coverage.
- Added per-agent skill assignment management across the API, runtime, tools, and UI.
- Added Anthropic usage and rate-limit reporting, adaptive reasoning support for Claude Fable 5.1, and model-registry drift visibility.

### Changed

- Improved provider reasoning, fast-mode handling, prompt estimation, and session-scoped runtime behavior.
- Improved Matrix rich-message formatting and model settings diagnostics.

### Fixed

- Fixed direct-turn tool dispatch fencing and task dispatch compatibility across concurrent sessions.
- Fixed Anthropic prompt caching, duplicate replay, deterministic rejection handling, and unbound wire tool-name recovery.
- Hardened MCP OAuth network destinations and bounded untrusted LSP server output, startup, cancellation, and telemetry.
- Fixed recoverable direct turns, stale stream retirement, and Chat v2 runtime authority behavior.
- Fixed session runtime diagnostics and cached-token layout in session details.

## [0.14.2] - 2026-09-11

### Fixed

- Fixed runtime authority lifecycle typing in the Chat v2 relay and scheduler.
- Fixed embedded documentation so bundled WebP screenshots resolve correctly.
- Fixed Anthropic tool schema compilation for explicit object roots with cross-field `oneOf` and `anyOf` constraints.

## [0.14.1] - 2026-09-11

### Changed

- Clarified project-specific harness instructions and orchestration guidance.
- Improved task and conversation Work activity grouping and scope identity.

### Fixed

- Fixed Chat v2 runtime authority ordering, durable revisions, scoped overlay reconciliation, and read-tail behavior.
- Fixed rich step deliverable hydration and truncated memory result previews.
- Fixed plain-text selection copying so copied text does not include Markdown syntax.
- Fixed executor rollout recovery and kept delegated Work graphs single-rooted.

## [0.14.0] - 2026-09-09

### Added

- Added focused child-session diagnostics, bounded nested-session discovery, and improved scoped Work activity inspection.
- Added richer executor capability reporting and safer targeted-executor path routing.

### Changed

- Improved managed channel lifecycle controls, delivery recovery, and route occupancy diagnostics.
- Improved compact dashboard chat windows, conversation inspector state, and rich deliverable navigation across responsive layouts.
- Improved Work projection seeding, materialization recovery, and browser release coverage.
- Improved direct web search with bounded DDGS engine selection, diagnostics, and deadlines.

### Fixed

- Fixed managed channel recovery and delivery ordering across controller and remote executors.
- Fixed stale focused-session identity, nested overview synchronization, compact outbox drains, and historical runtime attribution.
- Fixed rich deliverable SVG identifier collisions while preserving internal Mermaid references.
- Fixed browser regressions in mobile controls, Task Cockpit navigation, standalone deliverables, file history, rotation, and Work refresh behavior.

## [0.13.0] - 2026-08-09

### Added

- Added durable Work projections, scoped inspection, file evidence, activity trees, and responsive workspace navigation for task and conversation execution.
- Added explicit channel recipient resolution, managed channel conversations, observed message history, and artifact delivery across supported adapters.
- Added knowledgebase sharing, lifecycle recovery, document follow-ups, and richer browser and management surfaces.
- Added workflow task-cockpit controls, persistent task control chats, deterministic step execution, and reusable owner artifacts.

### Changed

- Improved Chat v2 synchronization, cache behavior, high-availability recovery, context management, executor routing, and provider telemetry.
- Improved local model discovery and reconciliation, rich deliverable rendering, web extraction reliability, and responsive mobile UI behavior.

### Fixed

- Fixed managed conversation routing, task and channel delivery, durable turn recovery, session navigation, and Work projection consistency.
- Fixed streaming, cache, browser, provider, migration, and background-repair races across controller and UI surfaces.

## [0.12.0] - 2026-07-25

### Added

- Added publication-grade rich deliverables with canonical charts, artifact-backed media, standalone exports, authenticated sharing, PDF rendering, and Pulse authoring contracts.
- Added native Anthropic Messages support, subscription authentication, server-side tool search, recursive tool schemas, and preserved native response envelopes.
- Added managed local-model discovery, provider-owned deployments, Ollama reconciliation, hardware capacity planning, and executor resource-health reporting.
- Added runtime profile management and switching, unified tool introspection, managed-conversation lineage operations, and durable delegate follow-up recovery.

### Changed

- Migrated conversation, session, and task-step presentation to the scoped Chat v2 protocol with shared rendering, deterministic grouping, and improved streaming performance.
- Improved context projection, idle compaction, executor reconnect handling, channel delivery, MCP OAuth lifecycle, and release validation coverage.
- Expanded rich UI presentation, mobile/PWA resilience, channel formatting, and operational diagnostics.

### Fixed

- Fixed duplicate or missing tool activity, attachment and deliverable recovery, managed-conversation delivery, and stale Chat v2 state across reconnects and navigation.
- Fixed executor retry and browser lifecycle races, provider routing edge cases, migration/bootstrap gaps, and local-model configuration consistency.
- Removed obsolete legacy WebSocket transport tests that could block the complete backend suite and refreshed unit fixtures for current orchestration contracts.

## [0.11.2] - 2026-07-06

### Fixed

- Fixed workflow step evaluation so persisted deliverable content is used instead of stale transient output.
- Fixed oversized context projection handling to avoid entering critical mode solely because of a single large tool result.
- Improved the managed-conversation mobile footer layout in the chat UI.

## [0.11.0] - 2026-07-05

### Added

- Added the Chat v2 realtime sync architecture with runtime overlay frames, REST snapshot/sync/backfill endpoints, and invariant coverage for ordering, deduplication, reconnection, and final-state preservation.
- Added a deterministic mock-LLM E2E harness, browser replay scenarios, and expanded golden streams for streaming, tool-output, reconnect, and multi-phase assistant flows.
- Added Matrix channel support and richer channel/account controls, including group-room handling, authenticated media download, thread overrides, and improved formatting.
- Added Anthropic subscription OAuth support, executor-routed inference fields, live context-usage streaming, chat display preferences, and rich Chat v2 tool/thinking/delegation rendering.

### Changed

- Reworked chat rendering around scoped Chat v2 projections, grouped activity/tool segments, stable runtime ordering, scroll stability, and shared session/task-step views.
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
