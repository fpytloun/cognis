# Stage 41 — Task Cockpit UX/UI Review & Redesign Proposal

## Status

**PROPOSAL — REQUIRES USER APPROVAL BEFORE ANY PRODUCTION UI IMPLEMENTATION.**

This document is a design review of the shipped Stage 39/40 Task Cockpit,
Chat v2 Work view, Task Control Chat, and workflow builder. It proposes a
coherent redesign that reuses existing primitives and preserves all current
product semantics. No production UI or backend code is changed by this stage.
The design worktree contains only `docs/` artifacts.

- Design worktree: `/home/riker/src/cognis-stage41-ux-review`
- Branch: `design/stage41-cockpit-ux-review`
- Review base/head: `f65280a4`
- Artifacts: this spec + `docs/wireframes/stage-41/` (SVG + PNG).

### Revision — user-approved direction + two refinements

The user **approved the overall Stage 41 decision-first direction** and asked
for two refinements, now folded into this spec:

1. **Task Agent Dock (task-detail scope only).** Promote the Task Control drawer
   (F3) into a calm, support-widget-style floating agent presence owned by the
   **task-detail layout** (`routes/(app)/tasks/[taskId]/+layout.svelte`),
   available **only within task detail** — the Cockpit, its internal tabs, step
   inspector, and Work modal/drawer. It is unobtrusive, keyboard-accessible,
   minimizable, dockable, and expandable to full-screen Chat v2. It binds to the
   task in the URL; navigating **out** of task detail unmounts the dock, and the
   persistent server-side conversation reopens on return. Task A → task B rebinds
   to B. Because the dock cannot exist off its task route, a mutation can never
   hit the wrong task. Full design: **§9**. This **supersedes** the standalone
   right drawer (now the dock's **docked-open state**, §9.2) **and removes** the
   earlier app-shell-singleton / follow-across-routes / detach / global-scope
   direction.
2. **Hierarchical Work file explorer.** Add a GitHub-PR-style changed-files
   **tree** (folders, per-node status/counts, filter/search, collapse/expand,
   selection, keyboard nav, synchronized diff scroll) to the Work view, derived
   **client-side** from the authorized Work projection paths. Full design:
   **§10**.

Both refinements remain design-only, reuse existing primitives (the persistent
task-control conversation, Chat v2 timeline/composer/queue, `WorkView`, and the
task-management tools), and introduce **no new chat model**. New wireframes
07–12 (§Appendix A) illustrate them.

## Scope reviewed

- Screenshots: `docs/screenshots/stage-39/*.png` (11 states, full resolution).
- Route + components: `ui/src/routes/(app)/tasks/[taskId]/+page.svelte`,
  `ui/src/lib/components/task-cockpit/*`, `ui/src/lib/components/work/WorkView.svelte`,
  `ui/src/routes/(app)/workflows/+page.svelte`, Chat v2 Work/session components.
- Specs: `37-workflow-task-cockpit.md`, `34-deterministic-workflows.md`,
  `implementation/stage-39-workflow-task-cockpit.md`,
  `implementation/stage-40-system-workflows.md`, `14-workflow-engine.md`,
  `implementation/stage-6a-workflow-context.md`.
- Tests: `ui/e2e/task-cockpit.spec.ts`, `ui/e2e/task-cockpit-fixture.ts`.

## Method

Heuristic audit (Nielsen 10 + information hierarchy + action priority),
journey mapping across all lifecycle states, comparison against the project's
own approved Spec 37 information hierarchy, and best-in-class pattern research
(Appendix B). Findings are evidence-linked to files, line ranges, and
screenshots. Wireframes express the target IA; they are not final visuals.

---

## 1. Executive summary

The Stage 39 build is **functionally complete and information-rich**, and one
screen — the completed **Task Work** view (`desktop-completed-work.png`,
`chat-v2-work.png`) — is genuinely excellent: a clear result hero, then metric
tiles, then a real diff and command output. That screen should become the model
for the rest of the cockpit.

The core problem is **hierarchy and duplication, not missing capability**. The
build inverts its own approved hierarchy (Spec 37 §"Information hierarchy") by
rendering the collaboration/comments composer first and pushing the
action-required gate below it, and it exposes **three overlapping ways to
resolve one pause**. The result is a long, flat, high-effort surface where the
single most important thing — the decision the human must make — is neither
first nor singular.

Recommended direction: **keep the runtime and every capability; re-sequence and
de-duplicate the presentation.** Make Attention the hero, collapse the three
pause-resolution surfaces into one decision panel (mirrored in the dock),
promote the completed Work view to an inline result hero, promote the
full-screen control-chat iframe into a task-scoped **Task Agent Dock** (§9) that
can actually resolve the pause and stays available throughout task detail, add a
hierarchical file tree to the Work view (§10), and split the 500+ line workflow
form into a scannable phase/step canvas with an advanced inspector. This is
achievable **frontend-only** for P0 and P1.

---

## 2. Findings (severity-ranked)

Severity: **S1** = blocks core task, causes wrong action, or violates the
approved spec · **S2** = significant friction/inconsistency · **S3** = polish.
Each finding cites evidence and a fix reference (§ or wireframe).

### S1 — Critical

**F1. Triple-redundant pause resolution.**
On the paused desktop overview (`desktop-paused-overview.png`) the user can
resolve the same pause in three places: the header **"Answer and resume"**, the
**COLLABORATION** card's **"Answer pause"** tab with its own textarea + "Submit
answer", and the **WORKFLOW GATE** card's "Optional instruction for the next
attempt" textarea + **Approve/Reject/Stop**. Two large textareas ask for
effectively the same guidance, stacked. This is ambiguous ("which one do I
use?"), error-prone, and inflates the page.
Evidence: `+page.svelte:1955-2105` (comments/collaboration order-1, attention
order-2); `TaskComments.svelte`; screenshots `desktop-paused-overview` /
`desktop-attention-controls`.
Fix: one unified Attention/Decision panel; header action just focuses it; the
collaboration composer becomes notes-only. → §5, wireframe 02.

**F2. The build inverts its own approved information hierarchy.**
Spec 37 §"Information hierarchy" mandates order Header → Objective →
**Attention (#3)** → Workflow → Step inspector → **Task context (#6,
comments/revisions)**, and §"UX rules": *"The active/waiting state and required
user action are visible above the fold."* The implementation renders
`TaskComments` (collaboration) at CSS `order-1` and the Attention area at
`order-2` (`+page.svelte:1955-1963`, `1965-2105`), so on a paused task the
first panel is "No comments yet." and the gate sits below it — frequently below
the fold on laptop viewports.
Fix: restore the spec order; Attention is the hero when action is required.
→ §5, wireframes 01/02.

**F3. "Task Control Chat" cannot control the task.**
In `desktop-control-chat.png` / `mobile-control-chat-reopened.png` the user can
ask "What is blocking this release?" and is told the task waits for approval —
but there is **no way to approve from the chat**. The user must close the
full-screen overlay and find the gate. A control surface that cannot act is a
dead end at the exact moment of need.
Evidence: `TaskControlChat.svelte:45-76` (iframe embed, links out to Work/Full
chat only).
Fix: promote to the task-scoped **Task Agent Dock** (§9) whose docked-open state
pins the same decision panel as §5, can resolve the gate in place, and stays
available across every task-detail surface (unmounting when the user leaves the
task). → wireframes 04, 07–10.

**F4. Completed result is split and contradictory.**
On a completed task the right-rail **RESULT** shows *"This task has not produced
a final result yet."* (`desktop-paused-overview.png` rail; `+page.svelte:2656-2679`)
while the canonical `write_deliverable` ("Release approved") lives inside a
separate **Task Work** modal reached via **"Explore"**
(`desktop-completed-work.png`, `TaskWorkPanel.svelte:122-141`). The single most
valuable output is hidden behind a click and contradicted by the rail.
Fix: render the canonical deliverable inline as the result hero; the rail
"Result" becomes a mini that scrolls to it. → §5, wireframe 03.

### S2 — Significant

**F5. Header action overload with no priority.**
The desktop header shows five equal-ish buttons — Re-run task, **Answer and
resume**, Configure, Task control chat, **Cancel task** (rose) — with the
destructive Cancel the most saturated element (`desktop-paused-overview.png`,
`+page.svelte:1817-1855`). No single primary; destructive over-weighted.
Fix: one state-contextual primary + overflow menu; destructive demoted inside
overflow with confirm. → §5, wireframes 01/02.

**F6. Terminology sprawl.** The same concepts have many names:
- Resolve a pause: "Answer and resume", "Answer pause", "Submit answer",
  "Guide next agent cycle", "Request revision", "Note", "Optional instruction
  for the next attempt".
- Talk to the agent: "Task control chat", "Chat about step", "Full chat",
  "Open chat".
- Run metrics: "Total step runs", "Eval revisions", "Review loops",
  "Re-executed steps" with bare `(?)` tooltips (`desktop-paused-overview.png`
  Statistics; `+page.svelte:2589-2654`).
Fix: a controlled vocabulary (§6.4) — one verb per concept, plain-language run
health with hover definitions.

**F7. "Statistics" rail is jargon in prime real estate.**
Origin/Timing/Statistics/Result occupy the whole right column on every state,
yet the highest-value item there (Result) is empty/contradictory (F4) and the
metrics are engine-internal terms. → demote to "At a glance" + "Run health"
with definitions; give the reclaimed space to Attention/Result. → wireframes.

**F8. Workflow builder is a single 500+ line flat form.**
`workflows/+page.svelte` renders every field of every step inline
(`+page.svelte:858-1447`; `workflow-builder-deterministic-step.png`): prompt,
input, deterministic routing, tool name, two JSON textareas, summary template,
safety flags, timeouts, redaction, outcome routing — all expanded at once.
There is no componentization and no progressive disclosure. Authoring a
5-step workflow is overwhelming and error-prone (raw JSON with no schema help).
Fix: phase/step canvas (names + one-line summary) + right inspector with
collapsible groups + inline validation. → wireframe 06.

**F9. No calm "running, nothing needed" state.**
When a task is running with no pause, the Attention slot is simply absent and
the collaboration composer still leads, so a healthy running task reads as
"empty/!" rather than "progressing". `desktop-progress-phases.png` shows live
work ("Independent evidence review · RUNNING", todos) buried below Work metrics.
Fix: a calm activity strip that surfaces current live work near the top. →
wireframe 01.

**F10. Duplicated Work surfaces with divergent styling.**
`TaskWorkPanel` (compact + modal) and Chat v2 `WorkView`
(`chat-v2-work.png`) render the same evidence with different chrome. Two
implementations drift and double the maintenance/test surface.
Fix: one `WorkView` used inline, in the modal, and in the drawer tab. → §6.

**F11. Multiple competing chat entry points.**
"Task control chat" (header), "Chat about step" (phase card,
`desktop-progress-phases.png`), "Open chat" (step), "Full chat" + "Work"
(overlay header) route to different scopes with different affordances.
Fix: all chat entry points open the single Agent Dock (§9) with the correct
scope preselected (task vs step). → wireframes 04, 07–10.

### S3 — Polish

- **F12.** Mobile control-chat header truncates the title to "Tas…"
  (`mobile-control-chat-reopened.png`); give the sheet a compact title row. →
  wireframe 05.
- **F13.** Two full-width equal buttons Approve/Reject imply equal weight
  (`desktop-attention-controls.png`); use primary/secondary weighting. →
  wireframe 02.
- **F14.** `(?)` tooltip affordances are tiny and non-obvious; use hover cards
  with a one-line definition and an example.
- **F15.** Diff/command cards are strong but dense; add copy-path and
  wrap/soft-wrap affordances (already partly present via ToolOutputDrawer).

### Screens that are NOT meaningfully better than a plain task page (honest call)

- **Paused desktop overview** (`desktop-paused-overview.png`): despite more
  chrome, it is *harder* to act on than a minimal "here is the decision" page,
  because of F1/F2/F5. This is the weakest screen and the main target.
- **Right rail Statistics/Result** (all states): net-negative — jargon plus a
  contradictory Result (F4/F7). A plain page with no rail would mislead less.
- **Task Control Chat** (`desktop-control-chat.png`): visually clean but
  strategically empty — it cannot do the one thing its name promises (F3).

### Screens that are already good (keep and propagate)

- **Completed Task Work** (`desktop-completed-work.png`, `chat-v2-work.png`):
  result hero → metric tiles → real diff → command with exit code and "Full
  output". This is the model for the whole cockpit.
- **Phase rail + typed step cards** (`desktop-progress-phases.png`): clear,
  backend-owned, matches Spec 37 §"Runtime example". Keep; only relocate.
- **Mobile lifecycle action sheet** (`mobile-paused-overview.png`): the
  primary-action + overflow pattern and per-state parity are correct.

---

## 3. Current product semantics preserved (non-negotiable)

The redesign changes presentation only. It preserves:

1. Canonical `write_deliverable` as the final task result.
2. First-class: task brief, attention (gate/step-question/credential/failure),
   collaboration/context/revision, phases/steps, todos, delegated/managed work,
   deterministic evidence, Work view (diffs/commands/mutations), persistent Task
   Control Chat, and desktop/mobile lifecycle actions with tested parity.
3. Backend-owned projections: phase status, step projection, attempt
   supersession, routing skips (Spec 37 §"Phase Projection"). The frontend must
   not re-derive these.
4. Stage 40 continuity: Research and Software Development keep primary steps in
   **one continued session**; architecture/code reviews remain **isolated**
   secondary sessions; reviewer decisions use `decision=approved|revise` with a
   five-iteration route budget; managed workstreams may run in parallel and are
   surfaced (not hidden).
5. Deterministic contract (Spec 34): `tool_call`/`condition`/`complete` steps,
   redaction, "no silent re-execution of side effects", lazy heavy data.
6. Persistent control-chat rules: reopen restores transcript; Star/Archive/
   Delete suppressed in task-control mode; Search/Info/Work retained
   (`implementation/stage-39-...md:581-582`).

**Explicitly NOT introduced:** no new intervention/revision/chat model, no
graph/DAG canvas, no `workflow_runs` table, and **no small-model progress
summary** (raw structured progress from the existing projection is sufficient;
see F9 fix, which only re-orders existing data).

---

## 4. End-to-end journeys

Each journey lists the trigger, the target experience, and the components/state
involved. "→ WF n" points to a wireframe.

1. **Draft refinement.** Draft task, no workflow yet. Cockpit shows Objective +
   Configure + "No workflow assigned" state; primary = **Submit**. Control
   Agent Dock available to discuss scope. (Spec 37 workflow-less fallback.)
2. **Running, multi-workstream.** Primary step running with a managed reviewer
   child. Calm activity strip + active phase auto-expanded; managed child links
   into the Agent Dock (§9), not a separate iframe. Primary = **Pause**. → WF 01.
3. **Queued human guidance (no pause).** User wants to steer a running task.
   Task-context composer posts a **note** (guidance) without a decision; if the
   step supports mid-run steering it is delivered as an agent-visible note.
   Never rendered as a gate. (Preserves "Guide next cycle" semantics as a note.)
4. **Waiting at a gate / step question.** Task paused. Attention hero is first
   and largest, with inline evidence (result/diff/tests) so the user decides in
   place; one optional-instruction field; primary **Approve & continue**. → WF 02.
5. **Paused → resume.** Same panel; "Approve & continue" resumes with the pinned
   snapshot. Header primary mirrors as "▲ Review the gate". → WF 02.
6. **Review rejection / revision.** "Reject / revise" reveals the same single
   instruction field with intent = revise; posts `decision=revise`, routes back
   per the workflow (Stage 40 budget respected, shown as "Review loop 2/5"). → WF 02.
7. **Completed result.** Terminal success. Canonical deliverable renders inline
   as the green hero; Work inline below; rail Result deep-links to it. → WF 03.
8. **Repeated revision.** Multiple review loops. Attempts and loop count shown in
   Run health and in the step inspector attempt switcher; the hero always shows
   the *latest* attempt with a compact "attempt 2 of N" control.
9. **Work inspection.** From result hero, Attention evidence, step card, or the
   dock "Work" tab — all open the same `WorkView` scope (task or step), now with
   the hierarchical file tree (§10). → WF 03/11/12.
10. **Task Control Chat via the Task Agent Dock.** Minimized FAB → docked-open
    panel (desktop) / bottom sheet (mobile) → full-screen Chat v2, Chat · Work
    tabs, pinned decision card when a pause is active, task title always visible;
    the dock is available throughout task detail and unmounts on leaving it
    (§9). → WF 04/07/08/09/10.
11b. **In-task steering.** User moves between the Cockpit, a step inspector, and
    the Work modal; the dock (minimized or open) stays available and keeps the
    same task; the user approves the gate from the dock without hunting for the
    Cockpit gate. Leaving task detail unmounts the dock. → WF 07.
11. **Workflow authoring.** Phase/step canvas + advanced inspector; validate
    targets/templates before save; YAML import/export and system overrides
    preserved. → WF 06.

Race/stale/reconnect variants are covered by the test plan (§8).

---

## 5. Information architecture

One **primary column** (decision → understanding), one **context rail**
(at-a-glance), one **Task Agent Dock** (task-scoped chat + work + inline
decision; §9). Full map:
`docs/wireframes/stage-41/00-information-architecture.svg`.

### Region model (desktop ≥ `lg` 1024px)

| # | Region | Always visible? | Container |
|---|--------|-----------------|-----------|
| 1 | Header (title, status, one primary action, overflow) | yes | inline |
| 2 | Objective (input/output, collapsible metadata) | yes | inline |
| 3 | **Attention / Decision** | only when action required (then hero) | inline |
| 3b| Activity strip (calm live status) | only when running, no pause | inline |
| 4 | Workflow (phase rail + active phase; others collapsed) | yes | inline, disclosure |
| 5 | Result + Work (result hero on terminal; Work inline; "Open full" → modal) | terminal: yes | inline + modal |
| 6 | Task context (notes/revisions, deps, config, activity) | collapsed | disclosure |
| R | Context rail (At a glance, Run health, Result mini) | yes ≥ lg | rail |
| D | **Task Agent Dock** (Chat · Work, inline decision) — task-detail-owned | on demand | docked-open state of the task-scoped dock (§9) / mobile sheet |
| I | Step inspector (attempts, output, eval, logs, chat) | on demand | drawer/panel |

The **Task Agent Dock (D)** is the one place to talk to and steer the task. It is
owned by the task-detail layout (§9): it is present on the Cockpit and every
other task-detail surface (tabs, step inspector, Work modal) in a minimized or
open state, and it **unmounts when the user leaves task detail**. Its **Work**
tab hosts the hierarchical file explorer (§10).

### Mobile (< `lg`)

Single column, same order. Sticky top bar = one contextual primary + `⋮`
overflow sheet (per-state lifecycle actions, tested parity). Context rail
collapses into a "Details" disclosure below Workflow. Step inspector and Control
drawer are full-screen `Sheet`s. No horizontal overflow; tap targets ≥ 44px.

### Visibility contract

- **Always visible:** header, objective, workflow phase rail, and (terminal)
  result hero.
- **Conditionally hero:** Attention (action required) or Activity strip
  (running). Exactly one leads the body at a time.
- **Collapsible:** completed phases, task context, technical metadata, run
  health definitions.
- **Modal:** full Work ("Open full"), configuration (`BlockingDialog`),
  destructive confirmations.
- **Drawer/sheet:** Control chat (+ Work tab) and step inspector.
- **Deep-linked (preserve existing):** `/chat/{id}?window=1&taskControl=1`,
  `?view=work`, and a new `#attention` / `#result` in-page anchor so
  notifications can jump straight to the decision or the result.

### Duplication resolution (maps F1/F4/F10/F11)

- **One pause-resolution surface** (Attention/Decision). The header primary
  action and the waiting gate step in the phase rail are *pointers* to it, not
  second controls. The Agent Dock shows the same decision component.
- **Collaboration composer → notes only.** "Note / Guide next agent cycle /
  Answer pause / Request revision / Optional instruction" collapse into: one
  Attention decision (with optional instruction) + one task-context **note**
  composer. No second textarea competes with the gate.
- **One Work component** (`WorkView`) used inline, in the modal, and in the
  dock's Work tab — now wrapped by the hierarchical file explorer (§10).
- **One chat entry** (the Agent Dock, §9) with scope preselected. Every legacy
  entry point ("Task control chat", "Chat about step", "Open chat", "Full chat")
  opens the same dock at the correct scope.

---

## 6. Component architecture, tokens, and implementation plan

### 6.1 Reuse map (existing → role in redesign)

| Existing | File | Redesign role |
|---|---|---|
| Route shell/state/polling/actions | `tasks/[taskId]/+page.svelte` | keep logic; re-sequence layout; extract panels |
| `TaskBrief` | `task-cockpit/TaskBrief.svelte` | Objective (region 2), unchanged |
| `WorkflowPhases` | `task-cockpit/WorkflowPhases.svelte` | Workflow (region 4); auto-collapse non-active |
| `TaskProgressPanel` | `task-cockpit/TaskProgressPanel.svelte` | feeds Activity strip (3b) + active-phase todos |
| `TaskWorkPanel` | `task-cockpit/TaskWorkPanel.svelte` | thin wrapper over shared `WorkView` |
| `WorkView` | `components/work/WorkView.svelte` | single Work implementation (inline/modal/drawer) |
| `TaskControlChat` | `task-cockpit/TaskControlChat.svelte` | becomes the **content adapter** rendered inside the Task Agent Dock (§9); hosts the decision card; no longer a bespoke full-screen iframe container |
| Task-detail layout | new `routes/(app)/tasks/[taskId]/+layout.svelte` | mounts the task-scoped `TaskAgentDock`; the dock mounts/unmounts with the task route; consumes shell z-band + safe-area offsets (does not own them) |
| `Sheet`, `BottomTabBar`, `ToastViewport` | `components/*` | dock reuses `Sheet` for mobile; must not collide with `BottomTabBar`/toasts (§9.6) |
| `TaskComments` | `components/tasks/TaskComments.svelte` | Task context notes (region 6); remove pause-resolution tabs |
| primitives | `components/ui/{Button,Card,Sheet,BlockingDialog,Badge,Popover,Tooltip}.svelte` | reused as-is |
| `FileDiffViewer`, `ToolOutputDrawer`, `ToolCallBlock` | `components/*` | reused for evidence |
| Workflow route | `workflows/+page.svelte` | split into canvas + `StepInspector` groups |
| Form model | `lib/workflows.ts` | unchanged data model; new grouping only |

### 6.2 New components (small, focused)

- `task-cockpit/AttentionPanel.svelte` — unified gate/question/credential/
  failure decision with inline evidence + single optional-instruction field +
  weighted actions. **Single source of truth for resolving a pause.**
- `task-cockpit/ActivityStrip.svelte` — calm live status from the existing
  projection (no new data, no small model).
- `task-cockpit/ResultHero.svelte` — renders the canonical deliverable inline
  (reuses the rich/markdown deliverable renderer already used by the Work modal).
- `task-cockpit/ContextRail.svelte` — At a glance + Run health (with definition
  popovers) + Result mini.
- `ui/Tabs.svelte` — extract the ad-hoc tab pattern (dock, Work, inspector).
- `workflows/StepInspector.svelte` + `workflows/StepCanvas.svelte` — builder
  canvas + collapsible advanced groups.
- **`task-cockpit/TaskAgentDock.svelte`** — task-detail-owned dock (mounted by
  the `[taskId]` layout) owning dock **presentation** state (minimized/open/
  docked/full-screen/hidden), position, and focus (§9). The bound task is the
  route param, not stored scope. Thin: it hosts `TaskControlChat` and `WorkView`;
  it does not fetch or mutate directly, and it unmounts off task detail.
- **`lib/stores/agentDock.ts`** — dock store: `state`, `scope` (task id + label
  + kind), `unread`/`attention` counts, `position`, `size`. Single writer;
  route changes update `scope.route`, never silently the bound task (§9.3).
- **`work/WorkFileTree.svelte`** + **`lib/work/fileTree.ts`** — client-side
  path→tree derivation and the changed-files tree UI wrapping `WorkView`/
  `FileDiffViewer` (§10).

No new state machine for the task itself; all read from the existing task
summary, the persistent control conversation, and lazy detail/work endpoints.
The dock store holds only view state (which task is bound, open/closed), never a
second copy of task truth.

### 6.3 Design tokens

The app has no semantic token layer for the cockpit; it uses raw Tailwind
colors, while `rich-blocks.css` already defines a real token system
(`components/rich/rich-blocks.css:20-115`). **Recommendation:** add a small
semantic layer in `app.css` (extends existing `--theme-*` at `app.css:30-36`)
and map cockpit components onto it. Proposed tokens:

```css
:root{
  /* status/intent */
  --st-running:#38bdf8; --st-waiting:#f5b544; --st-ok:#4fd1a5;
  --st-failed:#f2657f;  --st-pending:#7d8b99; --st-skipped:#5b6b79;
  /* step type accents (runtime + builder share) */
  --step-agent:#7fd08a; --step-tool:#5cc2f2; --step-cond:#e2b25a;
  --step-gate:#f5b544;  --step-complete:#4fd1a5;
  /* surfaces */
  --surface-1:#0e1722; --surface-2:#111a26; --line:#26323f;
  /* text */
  --text-1:#d5dee7; --text-2:#7d8b99;
  /* spacing rhythm (cockpit) */
  --sp-1:4px; --sp-2:8px; --sp-3:12px; --sp-4:16px; --sp-6:24px; --sp-8:32px;
  --radius-card:12px; --radius-ctl:8px;
}
```

(Values above are the wireframe palette; exact hexes are a design decision for
approval — see Appendix C.) One accent, amber only for attention, green only for
success/result, rose only for destructive. Do not introduce new fonts; keep the
15px root (`app.css:51-62`) and 16px touch inputs (`app.css:64-79`).

**Addendum — Agent Dock z-band + offsets (§9.6).** Define a documented stack so
the dock never fights toasts or blocking modals:

```css
:root{
  --z-content:0; --z-dock-fab:60; --z-dock-panel:65;
  --z-toast:70; --z-blocking:80;   /* modals/confirm always win */
  --agent-dock-fab-size:52px; --agent-dock-panel-w:min(420px,92vw);
  --agent-dock-gap:16px;           /* FAB inset from shell edges/safe-area */
}
```

The dock FAB reserves `--agent-dock-fab-size` in the toast viewport bottom
offset; the mobile FAB adds the existing `--app-shell-*` + safe-area insets so it
clears `BottomTabBar`.

### 6.4 Controlled vocabulary (fix F6)

| Concept | Use everywhere | Retire |
|---|---|---|
| Resolve a pending gate | **Approve & continue** / **Reject / revise** | Answer and resume, Answer pause, Submit answer, Guide next agent cycle |
| Free-text steer (no decision) | **Note** | Guide next agent cycle (as a control) |
| Talk to the task agent | **Ask** (opens the Agent Dock at task scope) | Task control chat / Chat about step / Full chat / Open chat (as separate names) |
| The persistent agent presence | **Agent Dock** (product), "the agent" (in copy) | (new — one name only; not "widget", "assistant bubble", "Clippy") |
| Which task the dock affects | **Scope** ("Scoped to: <task>") | (new — always shown; never implicit) |
| Extra guidance on a decision | **Optional instruction** | Optional instruction for the next attempt (verbose) |
| Engine counters | **Run health** (Steps · Retries · Review loops · Skips) with hover defs | Total step runs, Eval revisions, Re-executed steps |

### 6.5 Priority plan

**P0 — highest impact, frontend-only, no API change (target first PR).**
- F2/F1: re-sequence to Spec 37 order; introduce `AttentionPanel`; demote the
  collaboration composer to notes; header = one primary + overflow (F5).
  Files: `+page.svelte` (order/CSS + action row), new `AttentionPanel.svelte`,
  `TaskComments.svelte` (remove pause tabs).
- F4: `ResultHero.svelte` inline on terminal; rail Result → deep link.
- F3 (dock, P0 slice): stand up `TaskAgentDock` mounted by a new task-detail
  layout with **minimized / open-docked / full-screen / hidden** states, bound to
  the current `[taskId]` route's persistent Task Control Chat, embedding
  `AttentionPanel` so the gate is resolvable in place. Keeps the existing Chat v2
  session/iframe plumbing; adds the task-scoped container, presentation store,
  and decision card. Scope = the task route; the dock unmounts on leaving task
  detail and reopens the persistent conversation on return. Files: new
  `routes/(app)/tasks/[taskId]/+layout.svelte`, new
  `task-cockpit/TaskAgentDock.svelte`, `lib/stores/taskAgentDock.ts`,
  `TaskControlChat.svelte`.
- F6/F7: controlled vocabulary + `ContextRail` with Run health + definitions.

**P1 — consolidation, calm states, dock polish, file tree (frontend-only).**
- F10: collapse `TaskWorkPanel` onto shared `WorkView`; inline Work on all states.
- F9: `ActivityStrip` for running/no-pause.
- F11: unify all in-task chat entry points onto the dock.
- **Dock polish + scope safety (§9.3):** per-task open/minimized memory and
  drag/dock position persistence; unread/attention badges; clean rebinding on
  task A → task B; A→B header transition; reduced-motion; mobile bottom-sheet
  that avoids `BottomTabBar`/toast collision (§9.6). No cross-route persistence,
  no scope-switch/detach UI (removed by construction — the route is the scope).
- **Work file explorer (§10):** `WorkFileTree` + `lib/work/fileTree.ts`;
  resizable tree+diff on desktop, files-drawer→diff on mobile; filter/search,
  collapse/expand, keyboard nav, synchronized scroll; status/rename/delete/
  binary/truncated/40+-file and non-git-mutation handling.
- F12/F13/F14: mobile sheet title, action weighting, definition popovers.
- Extract `ui/Tabs.svelte`.

**P2 — workflow builder and delight.**
- **No Cognis-wide dock primitive** (§9.9 non-goal). A generalized/global
  assistant is explicitly out of scope; if ever wanted it is a separate future
  proposal with its own scope-safety design, not a continuation of this work.
- F8: `StepCanvas` + `StepInspector` with collapsible groups + inline
  validation; keep YAML import/export, overrides, unsaved-change guard.
- Delight: subtle status transitions honoring reduced-motion; optimistic
  decision feedback; keyboard command surface (see §7 delight).

### 6.6 Backend / API needs (prefer none)

P0 and P1 are **frontend-only**. Optional, non-blocking backend items (open for
approval, not required):

- **B1 (nice-to-have).** Ensure the task summary projection already exposes
  enough for the Attention "inline evidence" (result summary + counts) without a
  detail fetch. If not present, a bounded addition to the existing summary
  (still lazy for heavy payloads) — no new endpoint. Verify against
  `TaskWorkflowProjection` (Spec 37) before proposing.
- **B2 (nice-to-have).** A stable `decision` echo on the resume/revise response
  so the UI can show "Review loop n/5" without re-deriving Stage 40 budget.
- **B3 (file tree, verify-first).** The client tree derives folders, status, and
  counts from the existing Work projection `file_diffs[].path`. Rename pairing,
  binary/generated flags, and truncation are only exact if the projection
  already carries `old_path`, `is_binary`, and `truncated` on each diff/mutation.
  If it does not, add these **fields to the existing Work projection item**
  (still lazy for heavy payloads) — no new endpoint. Until then the UI degrades
  gracefully (rename shown as delete+add; binary shown as "binary, no preview").
  Verify against `WorkProjectionResponse` / `FileDiffViewer` inputs first.
- **No** new persistence, no `workflow_runs`, no progress-summary model. The
  Task Agent Dock needs **no** backend change: it reuses the persistent
  task-control conversation and Chat v2 work/session APIs already in place.

Any backend change is a **separate, explicitly-approved** task; the design does
not depend on it.

---

## 7. Usability & monkey-test plan

Goal: prove the redesign is understandable, safe, reversible, and accessible
across realistic and adversarial conditions. Layered on the existing harness:
Playwright (`ui/e2e/`), the fixture (`ui/e2e/task-cockpit-fixture.ts`), and
ChatV2 invariant suites.

### 7.1 Scripted personas (task-based usability)

Run each persona through defined tasks; record success, time-on-task, errors,
and a post-task confidence rating.

- **Operator (approver):** "Approve the release." Success = one decision, no
  scroll-hunt, no wrong composer. Target: locate decision < 5s; 0 mis-clicks
  into a non-decision composer.
- **Reviewer (rejecter):** "Send it back and ask for tighter summary." Success =
  one instruction field, `decision=revise`, loop counter updates.
- **Builder:** "Add a deterministic fetch step before the gate." Success =
  add step, set tool, pass inline validation, save; no raw-JSON dead-ends.
- **Observer (mobile):** "Check what's happening on your phone." Success = live
  status above the fold, no horizontal scroll.
- **Auditor:** "Show me exactly what changed and the command that proved it."
  Success = result hero → diff → command exit code without leaving the page.

### 7.2 Monkey / property tests (randomized valid sequences)

Drive the fixture through randomized but valid action sequences and assert
invariants after every step.

- **State-transition fuzz:** random walks over draft→running→paused→(resume|
  reject|stop)→completed→rerun. Invariants: exactly one hero (Attention *or*
  Activity *or* Result); exactly one pause-resolution surface visible; primary
  action matches state; destructive actions always require confirm.
- **Action-parity property:** for every lifecycle state, the mobile overflow
  sheet exposes exactly the tested action set (draft: Submit/Configure/Chat/
  Cancel; running: Pause/Configure/Chat/Cancel; paused: Resume/Configure/Chat/
  Re-run/Cancel; completed: Configure/Chat/Re-run) — extends
  `task-cockpit.spec.ts:80-207`.
- **Idempotent decision:** double-submit/double-approve results in one request
  (guard); reopening the drawer restores the transcript (persistence contract).
- **No-duplicate-control:** assert the gate decision DOM node is unique across
  header/attention/rail/dock (guards against F1 regressing).
- **Dock scope-safety (§9.3) — highest-value invariant.** Random walks that mix
  route navigation (task A → other route → task B → chat → back) with dock
  actions (open/minimize/send/resolve). Assert after every step: the dock is
  present **iff** the route is `/tasks/[taskId]`; the dock header task always
  equals the route `taskId`; any send/gate-resolve request targets **that** task
  id and no other; leaving task detail unmounts the dock (no off-route send is
  ever possible); task A → task B rebinds to B with no residual A state. This is
  the property that makes the scope correct by construction.
- **Dock single-instance / single-state:** at most one dock in the DOM (zero off
  task detail); exactly one of hidden/minimized/open/full-screen while mounted;
  dock suppressed whenever `blockingOverlayActive`.
- **File-tree fidelity (§10):** for a random authorized `file_diffs[]` set, the
  derived tree contains exactly the authorized paths (no invented nodes, no
  merged duplicates), folder counts equal the sum of descendants, non-git
  mutations never appear in the tree, and selection ↔ diff stay in sync.

### 7.3 Keyboard-only

Tab order = reading order (header → objective → attention → workflow → context).
Attention decision reachable without pointer; `Enter` = primary, `Esc` closes
dock/sheet, phase/step cards are arrow-navigable (Spec 37 frontend testing
requirement). **Task Agent Dock:** `A` toggles it (task-detail routes only),
focus moves into the panel on open and back to the FAB on close, FAB is last in
the task view's tab order, `Esc` collapses open→minimized without cancelling the
turn. **File tree:** roving-tabindex
`role="tree"`, `↑/↓/←/→`, `Enter`, `/`-to-filter (§10.4). Acceptance: complete
the Operator and Reviewer personas, open the dock, and navigate a 40-file tree
to a specific diff — all keyboard-only.

### 7.4 Narrow / mobile / long content / scale

- 320–414px widths: no horizontal overflow (extends the existing containment
  checks `task-cockpit.spec.ts`); tap targets ≥ 44px.
- Long content: 4k-char deliverable, 40-file diff, 200-line command output,
  20 phases × 8 steps — sections scroll internally; page stays navigable; Work
  stays lazy.
- Many workstreams: 6 parallel managed children render as a bounded, scrollable
  list with per-child status, not an unbounded wall.
- **Dock collision (§9.6):** at 320–414px the FAB never covers `BottomTabBar` hit
  targets or the last toast; open dock uses the bottom `Sheet` at ≤88dvh; toasts
  reflow above the dock; blocking modal suppresses the dock.
- **File tree at scale:** 40-file / deep-folder change stays navigable on mobile
  (list→diff), no horizontal overflow, diff lazy.

### 7.5 Stale / race / error / reconnect

- 5s poll returns a superseded attempt mid-decision → UI reconciles to
  backend-owned projection; never shows a resolved gate as still pending.
- Decision submitted while a background refresh lands → optimistic state
  replaced by canonical, no flicker/duplicate (reuse ChatV2 invariants
  INV-STABLE-ORDER / INV-NO-DUP as the pattern).
- Work fetch fails on one step run → other runs still render; explicit
  retryable error, existing projection retained (`WorkView.svelte:58-97`).
- Dock/iframe/session reconnect (within task detail) → transcript and decision
  card restored from the persistent server-side conversation.
- Navigate away from the task while a decision is pending → dock unmounts; the
  normal app notification path still surfaces the attention; returning to
  `/tasks/{id}` reopens the same live decision in the dock.
- Task A → task B while a decision is pending on A → dock rebinds to B and shows
  B's state; A's pending decision is untouched and reappears on return to A.

### 7.6 Screen-reader & reduced-motion

- Landmarks/roles: Attention = `role="region"` `aria-label="Action required"`
  and `aria-live="polite"` when it appears; status pills have text equivalents;
  step glyphs have `aria-label` (Agent/Tool/Condition/Gate/Complete).
- `prefers-reduced-motion`: disable non-essential transitions; keep instant
  state changes. Acceptance: NVDA/VoiceOver can complete Operator + Reviewer.

### 7.7 Measurable acceptance criteria (definition of done for the redesign)

1. Time-to-locate the required decision (paused task, cold) **< 5s** median; 0
   wrong-composer clicks across ≥ 8 test users.
2. Exactly **one** pause-resolution surface in the DOM in every paused state
   (automated assertion) — F1 cannot regress.
3. First meaningful decision/element is **above the fold** at 1366×768 and
   390×844 (screenshot assertion) — satisfies Spec 37 §UX rules.
4. Completed canonical result is visible **without a click** and the rail never
   contradicts it (automated) — F4 fixed.
5. Gate is resolvable **from the Task Agent Dock** (persona + e2e) — F3 fixed.
6. 100% lifecycle action-parity preserved vs `task-cockpit.spec.ts`.
7. Keyboard-only + screen-reader completion of Operator & Reviewer personas.
8. No horizontal overflow 320–414px; tap targets ≥ 44px.
9. Builder: a 5-step workflow authored with **0 raw-JSON validation dead-ends**;
   invalid tool name / route target blocked before save.
10. Zero new backend endpoints for P0/P1; all existing e2e green.
11. **Dock scope safety:** across an automated navigation+action fuzz, **0**
    actions ever target a task other than the current `/tasks/[taskId]` route;
    the dock is present iff on a task-detail route; leaving task detail unmounts
    it; task A → task B rebinds with no residual A state. (Release-blocking
    criterion for the dock.)
12. **Dock single-instance:** at most one dock (zero off task detail), one active
    state while mounted; suppressed under any blocking modal; unread/attention
    badge matches the Attention hero.
13. **File tree:** authored tree == authorized paths (no invented/merged nodes);
    non-git mutations excluded; a target file in a 40-file change reachable in
    **≤ 3 interactions** (filter or expand+select) and keyboard-only; rename/
    binary/deleted/truncated each render their explicit state.

---

## 8. Recommended direction (one coherent bet)

Adopt the "**decision-first cockpit**": Attention (or, when running, a calm
Activity strip; when done, the Result hero) always leads; everything else is
progressive disclosure; the **Task Agent Dock** (§9) is the single, capable,
always-reachable place to talk to and steer the task **while inside task detail**
— a calm presence owned by the task-detail layout that unmounts when the user
leaves the task, so its scope is the route and can never target the wrong task;
the Work view is one component everywhere, now navigable via a **hierarchical
file tree** (§10); the builder is a scannable outline with an advanced
inspector. This is a re-sequencing and de-duplication of what already exists
plus one new task-scoped dock — low risk, frontend-first, fully within Spec 37's
approved model and Spec 34/Stage 40 semantics.

Sequencing: ship the task-scoped dock and the file tree in P0/P1. There is **no**
Cognis-wide dock primitive on the roadmap (§9.9 non-goal); a global assistant, if
ever wanted, would be a separate future proposal with its own scope-safety
design.

---

## 9. Task Agent Dock (task-detail scope only)

Refinement 1, scope-corrected. The Task Agent Dock is a calm, always-reachable
agent presence that lives **inside the task-detail experience only**
(`/tasks/[taskId]`). It is owned by the **task-detail layout, not the app
shell**. While the user explores that task — its internal tabs, step inspector,
Work modal/drawer, and every responsive state — the dock is always available as
a minimized launcher, a docked panel, or a full-screen task-control view. When
the user navigates **outside** task detail (Tasks list, Projects, Agents,
generic Chat, Workflows, Settings) the dock and its launcher **unmount and are
removed from view**. It is task-detail control UX, never a global assistant.

Design intent: enjoyable and unobtrusive, never a nag. It reuses the persistent
Task Control conversation, Chat v2 timeline/composer/queue, `WorkView`, and the
task tools. **No new chat model, no new session type, no app-shell singleton,
and no Cognis-wide primitive.** The task-control conversation is durable
server-side, so it survives the unmount and **reopens when the user returns to
that task**.

This supersedes the standalone control drawer of the first proposal (F3): the
drawer is now one **open state** of this task-scoped dock. It also **supersedes
and removes** the earlier "app-shell singleton / follows the user across routes
/ detach / global scope-switch / future Cognis-wide primitive" direction, which
is not part of this approved scope.

### 9.1 Ownership and lifecycle (the corrected contract)

- **Mounted only within task detail.** Provided by a new task-detail layout
  (`routes/(app)/tasks/[taskId]/+layout.svelte`) or the task-detail page shell,
  so it renders on the Cockpit and every in-route surface (task tabs, step
  inspector, Work modal/drawer) and nowhere else.
- **Unmounts on leaving task detail.** Navigating to any non-task-detail route
  removes the dock and its launcher. No "following" chip, no off-route presence.
- **Returns cleanly.** Re-entering `/tasks/{id}` reopens the same persistent
  transcript and decision state from the server-side conversation.
- **Task A → task B.** Navigating directly from task A detail to task B detail
  **rebinds to B's** control conversation; A is never silently retained. There is
  no cross-task scope-switch UI, because the route *is* the scope.
- **Per-task UI state.** The open/minimized choice may be remembered **per task
  id** for the session when that is unambiguous; otherwise the dock defaults to
  **minimized** each time a task is entered. UI-state memory never keeps a task
  "live" once its route is left.
- **Single source of scope truth: the route.** The bound task id always equals
  the current `/tasks/[taskId]` param. Because the dock cannot exist off that
  route, a mutation can only ever target the task the user is currently viewing.

### 9.2 Desktop states

The former right drawer = the **docked-open** state here. One state at a time.

| State | Trigger | Presentation | Notes |
|---|---|---|---|
| **Hidden** | Not on a task-detail route (dock unmounts); or chat window mode (`window=1`); or a `BlockingDialog`/`ConfirmDialog` is active | not rendered / suppressed | fully unmounts off task detail; never overlaps a blocking modal (`blockingOverlayActive`) |
| **Minimized (FAB)** | Default on entering a task, or user collapses | small rounded launcher, bottom-right within the task view, agent glyph + status ring + unread/attention badge | ~44–56px; draggable to a corner (§9.7); calm idle, gentle pulse only on new attention |
| **Open (docked)** | Click FAB / "Ask" / any chat entry point / keyboard `A` | right-docked panel (`min(420px, 92vw)`), non-modal, task view still scrolls/interacts | **Chat · Work** tabs; pinned decision card when a pause is active; header shows the current task title |
| **Full-screen** | "Expand" in the dock header | Chat v2 window mode (existing `?window=1&taskControl=1`) with a persistent task-scope title and a **Return to task** action | conceptually still task-detail control UX; "Minimize"/"Dock" returns to docked/minimized |

On the **Cockpit route**, "docked-open" is the region **D** from §5. On other
in-task surfaces (step inspector, Work modal) it floats in the same docked
position over that surface. It never appears outside task detail.

Reduced motion: `prefers-reduced-motion` disables the pulse and slide; state
changes are instant. Collapsed idle has no looping animation regardless.

### 9.3 Scope binding and safety (the core correctness rule)

**Invariant: the dock exists only on `/tasks/[taskId]`, and a mutation can only
ever target that route's task.** Scope is not a stored, mutable value that can
drift from the view — it *is* the route. This makes the wrong-task-mutation
class of bug impossible by construction rather than by guard.

- **Binding is implicit and unambiguous:** the dock binds to the `taskId` in the
  URL; the header shows that task's title.
- **Leaving task detail unmounts the dock:** there is no off-route scope that
  could be steered by mistake.
- **Task A → task B rebinds to B** (new route param); A is never retained. A
  brief header transition marks the change on direct A→B navigation.
- **No detach, no manual scope switch, no "following" chip, and no
  scope-mismatch banner** — all removed. They are unnecessary because the dock
  cannot outlive its task route.
- The dock store holds only **presentation** state (open/minimized, position,
  unread) keyed by task id; it never holds an independent task id that could
  disagree with the URL.

Monkey/property coverage for this invariant is in §7.2 (scope-safety) and the
acceptance criteria §7.7.11–12.

### 9.4 Badges, attention, and calm

- **Unread** count = new agent messages since the dock was last read at that
  scope (from the existing conversation read state; no new model).
- **Attention** (amber ring + dot) when the bound task is **waiting on the user**
  (gate/step-question/credential) — derived from the same projection that drives
  the Attention hero (§5). This is the only time the FAB may gently pulse (once,
  respecting reduced motion).
- No sound, no toast spam. On the task-detail route a single toast may announce
  "Task needs your approval" and its action **focuses the dock** (it does not
  open a second surface). Off the task route the dock is unmounted, so attention
  is surfaced by the normal app notification/toast path (unchanged existing
  behavior); tapping such a notification routes to the task and opens the dock.
  Attention badge and Attention hero never disagree (shared source).

### 9.5 Focus, keyboard, accessibility

- FAB is a real `<button aria-haspopup="dialog">` with `aria-label` including the
  scope ("Open agent for <task>"). Attention state adds `aria-description`.
- Open docked panel: `role="dialog"` `aria-label="Agent — <task>"`, **non-modal**
  (page remains operable) but focus **moves into** the panel on open and returns
  to the FAB on close. It is *not* a focus trap in docked mode (non-modal);
  full-screen mode traps focus like the current chat window.
- Shortcuts: `A` toggle dock (active **only on task-detail routes** where the
  dock is mounted; guarded by the existing `isTextInputTarget` and
  `blockingOverlayActive` checks in `handleGlobalShortcuts`), `Esc` collapses
  open→minimized (does not cancel the turn when the dock owns focus), `Ctrl/Cmd+
  Enter` sends. Tab order: FAB is last in the task view's tab order so it never
  precedes page content.
- Screen reader: state changes announced via a polite live region ("Agent
  docked", "scoped to <task>"); badge count exposed as text.

### 9.6 Collision policy (toasts, mobile nav, safe areas)

- **Toasts** (`ToastViewport`) and the **FAB** share the bottom-right. Policy:
  the FAB owns bottom-right; toasts stack **above** the FAB by reserving
  `--agent-dock-fab-height` in the toast viewport offset. Open/full-screen dock
  → toasts shift to top or above the dock edge.
- **Mobile `BottomTabBar`:** the FAB sits **above** the tab bar using the
  existing `--app-shell-*` offsets + `adaptiveBottomInset`, or (design option,
  §Appendix C) becomes a **center action in the tab bar** on ≤ `sm` to avoid any
  float. Never covers the tab bar's hit targets.
- **Blocking overlays:** dock is suppressed while `blockingOverlayActive` (config
  `BlockingDialog`, destructive `ConfirmDialog`) so it never competes with a
  modal decision.
- **Chat window mode** (`window=1`) and login/loading shells: dock hidden.
- z-band: content < FAB < docked panel < toasts-in-context < blocking modals.
  Documented as tokens in §6.3 addendum below.

### 9.7 Position and drag policy

- Default bottom-right. Draggable to any of 2 (mobile) / 4 (desktop) corners;
  snapped, not free-floating, so it never lands over the primary action or the
  Attention hero. Position persists in `localStorage` (like sidebar collapse).
- The docked panel is anchored to the FAB's horizontal side (right corners →
  right dock; left corners → left dock) and never exceeds `92vw`.
- Drag is pointer-based and keyboard-alternative (chip menu → "Move to corner").

### 9.8 Mobile states

- **Minimized:** single FAB above the `BottomTabBar` (or tab-bar center action).
- **Open:** bottom **`Sheet`** (reusing the app's `Sheet`) at ~88dvh with a
  compact title row showing the task title (fixes F12 truncation), **Chat · Work**
  tabs, and the pinned decision card. Swipe-down / `Esc` → minimized.
- **Full-screen:** the existing Chat v2 window sheet.
- Safe areas honored (`env(safe-area-inset-*)`), tap targets ≥ 44px, no
  horizontal overflow.

### 9.9 Explicit non-goal: no Cognis-wide primitive

A generalized, app-shell "assistant that follows the user across the product" is
**out of scope** and intentionally not designed here. The dock is task-detail
control UX. There is no `kind: 'chat' | 'project' | null` scope model, no
adapter registry, no detach-to-unscoped state, and no P2 generalization track.
If a global assistant is ever wanted, it is a separate future proposal with its
own scope-safety design; it is not implied or reserved by this work.

### 9.10 Component/state mapping (dock)

| Concern | Where | Reuse |
|---|---|---|
| Mount (task-detail only) | new `routes/(app)/tasks/[taskId]/+layout.svelte` (or task-detail page shell) | mounts/unmounts with the task route |
| Dock shell + states | new `task-cockpit/TaskAgentDock.svelte` | `Sheet`, `Button`, `Tabs` |
| Dock store (presentation only, keyed by task id) | new `lib/stores/taskAgentDock.ts` | pattern of `stores/mobileNav.ts`, `stores/overlays.ts` |
| Bound task id | route param `[taskId]` (single source of truth) | no stored scope |
| Task chat content | `task-cockpit/TaskControlChat.svelte` | existing session/iframe/composer/queue |
| Decision card | new `AttentionPanel.svelte` (§6.2) | shared with cockpit hero |
| Work tab | `work/WorkView.svelte` + `WorkFileTree` (§10) | shared |
| Attention/unread source | existing task projection + conversation read state | no new data |
| Offsets/collision | `--app-shell-*`, `adaptiveBottomInset`, new `--agent-dock-*` | existing shell vars (consumed, not owned) |

---

## 10. Work file explorer (hierarchical changed-files tree)

Refinement 2. Give the Work view a GitHub-PR-style **changed-files tree** so a
40-file change is navigable, not an endless scroll. Today `WorkView` flattens
`projection.mutations[].file_diffs` into one flat list and passes it straight to
`FileDiffViewer` (`WorkView.svelte:54-56, 154-161`). We wrap that with a tree
derived **client-side** from the diff paths.

### 10.1 Data derivation (client-side, no backend by default)

- Input: the authorized `WorkProjectionResponse` already loaded by `WorkView`
  (`file_diffs[]` with `path` + change status; `summary.changed_files`).
- `lib/work/fileTree.ts` folds `path` on `/` into a folder/file tree, carrying
  per-node **status** (added/modified/deleted/renamed) and **aggregate counts**
  (changed files, +adds/−dels when available) up each folder.
- Purely a projection of already-authorized paths → respects redaction: a path
  the backend did not send simply is not in the tree (§10.5).
- Backend fields are needed **only** for exact rename/binary/truncation
  (B3, §6.6); absent them the tree degrades gracefully.

### 10.2 Desktop layout

- Two-pane inside the Work tab / Work modal: **resizable tree (left, default
  ~280px, min 200 / max 480) + diff pane (right, fills)**. Splitter persists
  width in `localStorage`.
- Tree: collapsible folders, status color/glyph per node (reuse step-type/status
  tokens §6.3), aggregate count badges on folders, current-file **selection**
  highlight, sticky filter/search box at the top.
- Diff pane: the selected file's `FileDiffViewer`; **synchronized scroll** —
  scrolling the diff updates the active tree node; clicking a tree node scrolls
  the diff to that file (single-file view by default, "show all" optional).
- **Command** and **Mutation** views (F10 evidence) remain as sibling tabs/
  sections; the canonical **deliverable** hero stays above (unchanged). The tree
  governs the *changed-files* region only.

### 10.3 Mobile layout

- One column. Work tab opens on a **files list/drawer** (the tree, full width);
  tapping a file pushes a **full-width diff** with a back affordance to the list.
- Filter/search pinned at top of the list; folders collapsible; large diffs lazy.

### 10.4 Filter, search, keyboard, scale

- Filter box matches path substring and status filters (e.g. only deletions);
  empty-result state is explicit.
- Keyboard: `↑/↓` move selection, `←/→` collapse/expand folder, `Enter` opens/
  focuses the diff, `/` focuses filter. Selection is a roving-tabindex `tree`
  (`role="tree"`/`treeitem"`/`group`), fully operable without pointer.
- Scale: 40+ files render virtualized/collapsed-by-default at the top folder
  level; counts always visible so the user sees magnitude before expanding.

### 10.5 Edge cases (explicit handling)

| Case | Behavior |
|---|---|
| Duplicate filenames in different folders | distinct tree nodes by full path; never merged |
| Rename | with `old_path` (B3): single "renamed" node `old → new`; without: shown as delete+add, labeled "possibly renamed" |
| Deletion | node present with deleted status/glyph; diff shows removal; not hidden |
| Binary / generated | node marked "binary" / "generated"; diff pane shows "no text preview" + metadata, not a huge blob |
| Large / truncated diff | node badge "truncated"; diff pane shows the partial hunk + "Open full output" (existing `ToolOutputDrawer` pattern) |
| 40+ files | folders collapsed by default, counts shown, virtualized list, filter encouraged |
| Non-git mutation (`file_diffs.length === 0`) | **not** in the file tree; stays in the existing **Other mutations** section (`WorkView.svelte:190-208`) |
| Unauthorized / redacted path | absent from the projection → absent from the tree; no placeholder that implies hidden content beyond an aggregate "n files not shown" if the backend sends such a count |
| No changed files (commands only) | tree region hidden; Commands/Mutations/deliverable render as today |

### 10.6 Component/state mapping (file tree)

| Concern | Where | Reuse |
|---|---|---|
| Tree derivation | new `lib/work/fileTree.ts` (pure, unit-tested) | — |
| Tree UI + splitter | new `work/WorkFileTree.svelte` | `FileDiffViewer`, tokens §6.3 |
| Host | `work/WorkView.svelte` (wrap the "Changed files" section, `:154-161`) | unchanged data flow |
| Diff render | `components/FileDiffViewer.svelte` | as-is |
| Full output / truncation | `components/ToolOutputDrawer.svelte` | as-is |
| Deliverable hero, Commands, Other mutations | `WorkView.svelte` | unchanged |

---

## Appendix A — Wireframes

Source + PNG in `docs/wireframes/stage-41/` (see its `README.md`). Callout
numbers in the SVGs correspond to the annotations printed on each frame and to
the findings in §2 and the designs in §9–§10.

- 00 IA region model · 01 running desktop · 02 action-required desktop ·
  03 completed result + Work · 04 dock docked-open over Cockpit · 05 mobile
  cockpit + agent sheet · 06 builder.
- **07 minimized dock on a task-detail page** (task-scoped FAB; annotates
  route-unmount + persistent-conversation-reopen) · **08 open task-scoped dock
  over Cockpit** (Chat·Work, pinned decision card, task title) · **09 full-screen
  expansion** (Chat v2 window mode, Return-to-task) · **10 mobile agent sheet**
  (FAB above BottomTabBar → bottom sheet → full-screen) · **11 desktop Work tree
  + diff** (resizable, folders, status, filter, sync scroll) · **12 mobile file
  navigation** (files drawer → full-width diff).

## Appendix B — Research: borrowed principles (with sources)

Principles only; no branding. Sources gathered during review.

1. **Real-time run graph + drill-down to step logs.** Show run status as a live
   graph and let users open any step's log. (GitHub Actions —
   https://docs.github.com/en/actions/how-tos/monitor-workflows and
   .../use-the-visualization-graph)
2. **Monitor, steer, and trace agent sessions.** An agents panel shows progress,
   token/length, and lets you *steer* a running session without stopping it;
   commits link back to session logs for audit. (GitHub Copilot coding agent —
   https://docs.github.com/en/copilot/how-tos/copilot-on-github/use-copilot-agents/manage-and-track-agents)
3. **Review changes as a familiar diff; require explicit approval for risky
   actions.** Agent edits appear in a diff review UI; terminal commands need
   approval by default. (Cursor — https://docs.cursor.com/en/agent/review and
   https://docs.cursor.com/en/account/agent-security)
4. **Permission-based, read-only-by-default control.** Explicit approval before
   system-modifying actions; a known read-only set runs without prompts.
   (Claude Code — https://code.claude.com/docs/en/security)
5. **Plan-before-work with inspectable citations.** Surface the plan and the
   evidence (files/citations) the agent used, and let the human adjust before
   execution. (Devin interactive planning —
   https://docs.devin.ai/work-with-devin/interactive-planning)
6. **Linear version history for iterations.** Each iteration is a version; you
   can switch/restore; history stays linear and legible. (v0 —
   https://v0.app/docs/versions)
7. **DAG/run status derived from terminal step states; skipped is first-class.**
   Run status is computed from leaf/step terminal states; skipped is explicit,
   not "pending forever". (Airflow — https://airflow.apache.org/docs/apache-airflow/stable/core-concepts/dag-run.html)
8. **Skeletons for full-page loads, spinners only for short waits; show
   progress for long ones.** (NN/g — https://www.nngroup.com/articles/skeleton-screens/
   and https://www.nngroup.com/articles/progress-indicators/)
9. **Buttons for actions that progress a task; chips for filters/attributes —
   don't replace primary actions with chips.** (Material 3 —
   https://m3.material.io/components/chips/guidelines)
10. **Progress indicators must reassure that the system isn't stalled; live
    activity should be glanceable.** (Apple HIG —
    https://developer.apple.com/design/human-interface-guidelines/progress-indicators
    and .../live-activities)

Deduplicated transferable principles used in this proposal: decision-first
hierarchy; steer-without-stopping; act where you inspect (diff + approve
together); read-only-by-default with explicit approval for risk; plan/evidence
inspectability; linear, legible iteration history; backend-owned status with
first-class skipped/terminal derivation; calm glanceable live status; skeleton
over spinner for page loads; buttons (not chips) for progression; one component
per concept; progressive disclosure of advanced configuration.

**Anti-patterns avoided:** multiple controls for one action (F1); jargon in
prime real estate (F7); control surfaces that cannot act (F3); hiding the
primary result behind a click (F4); fully-expanded mega-forms (F8); full-screen
iframes that hide app navigation (F3); equal-weight buttons for unequal actions
(F13).

## Appendix C — Decisions requiring user approval

1. **Proceed to implementation?** This is a proposal; no production UI is
   changed until approved.
2. **Adopt the decision-first hierarchy** (Attention/Activity/Result as the
   single hero; collaboration demoted to notes). Confirms F1/F2 direction.
3. **Control chat becomes the Task Agent Dock that can resolve the gate**
   (F3, §9). Confirm: (a) task-detail-scoped dock (mounts/unmounts with
   `/tasks/[taskId]`, never follows the user off task detail), (b) inline
   decision card inside the dock, (c) the former right drawer is now the dock's
   docked-open state (not a separate component).
4. **Semantic token layer + controlled vocabulary** (§6.3/§6.4). Approve exact
   palette hexes and the retired/renamed labels (user-facing wording change).
5. **P0/P1 are frontend-only; B1/B2 backend items are optional.** Approve
   whether to also schedule B1/B2 or defer.
6. **Big-bang route cutover** remains per Spec 37 (no permanent legacy toggle) —
   confirm timing/appetite.
7. **Rename "Statistics" → "Run health"** and definition-popover wording
   (user-facing).
8. **Task Agent Dock scope policy (§9.1/§9.3) — CORRECTED.** Confirm the dock is
   **mounted only within task detail** (`/tasks/[taskId]` and its internal
   surfaces), **unmounts** when the user leaves task detail (no following, no
   detach, no scope-switch UI), reopens the persistent conversation on return,
   and rebinds task A → task B by route. Scope is the route by construction.
9. **No Cognis-wide dock primitive (§9.9).** Confirm a generalized/global
   assistant is **out of scope** for this work (removed from the roadmap), and
   would only ever be a separate future proposal with its own scope-safety
   design.
10. **Mobile dock placement (§9.6).** Choose: floating FAB above `BottomTabBar`
    (default) **or** a center action integrated into the tab bar on ≤ `sm`.
11. **Work file tree (§10) + B3 backend fields.** Approve the client-side tree by
    default, and decide whether to schedule **B3** (add `old_path`, `is_binary`,
    `truncated` to the Work projection item) now for exact rename/binary/
    truncation, or accept graceful degradation until later.
12. **Dock naming/copy.** Approve "Agent Dock" / "the agent" and the single
    scope-chip wording ("Scoped to: <task>"); reject cutesy framing
    ("assistant bubble", "Clippy").
