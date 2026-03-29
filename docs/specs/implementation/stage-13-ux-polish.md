# Stage 13: Core UX Polish

**Status**: DONE
**Repo**: `cognis` (primarily `ui/`)
**Depends on**: Stage 11 (flows must be stable before polishing surfaces)
**Estimated effort**: 4-5 days

## Objective

Remove daily friction from the main workflows. Every core action should
give immediate feedback and work cleanly across desktop and mobile devices.

## Context

The UI is functionally complete — every page is real and wired up — but it
lacks the polish expected of a usable product:

- Zero client-side form validation; users must submit and read API errors.
- No success feedback on save/create/delete operations.
- No confirmation on destructive actions (delete conversation, cancel task).
- Chat has no Enter-to-send, no timestamps, no "thinking" indicator.
- No icons anywhere (lucide-svelte is installed but unused).
- Sidebar disappears on mobile with no hamburger menu.
- No keyboard shortcuts.
- Accessibility deferred from Stage 8 (no ARIA labels, no skip links).

## Deliverables

### 1. Toast Notification System

Global feedback for all user actions.

- **Toast component**: slide-in notification at top-right or bottom-right.
  Variants: success (green), error (red), warning (amber), info (blue).
  Auto-dismiss after 4 seconds. Stacked when multiple. Dismiss on click.
- **Toast store**: Svelte store with `addToast(message, variant, duration)`
  API. Importable from any component.
- **Wired to all mutations**: save agent, create agent, delete agent,
  sync personality, save provider, test provider, delete provider, save
  routing, create/delete secret, save setting, create/submit/cancel task,
  save workflow, duplicate workflow, archive/delete conversation, resolve
  escalation.
- **Error toasts**: complement existing inline error banners for transient
  failures (network errors, 500s). Inline banners remain for persistent
  form errors.

### 2. Form Validation and Required Fields

Inline validation before submit on all forms.

- **Required field indicators**: red asterisk or "Required" label on
  mandatory fields across: setup form, agent form (agent_id, name),
  provider form (provider_id, display_name), task form (title, agent_id),
  workflow editor (workflow_id, name, step names).
- **Inline error messages**: shown below the field on blur or submit.
  Examples: "Agent ID is required", "Password must be at least 8
  characters", "Invalid JSON syntax".
- **Disable submit**: submit button disabled until required fields are
  filled and validation passes.
- **JSON validation**: for raw JSON textareas (custom provider config,
  step agent overrides, extra routing), validate syntax on blur and show
  error with line/position if invalid.

### 3. Confirmation Dialogs

Prevent accidental destructive actions.

- **Confirmation modal component**: title, message, confirm button
  (danger variant), cancel button. Keyboard accessible (Escape to cancel,
  Enter to confirm).
- **Applied to**: delete conversation, delete agent, cancel task, remove
  task dependency, delete secret, delete provider, delete workflow.
- **Unsaved-changes protection**: on agent form, workflow editor, and
  settings forms — warn before navigating away with unsaved changes.
  Use `beforeunload` event and SvelteKit `beforeNavigate`.

### 4. Chat UX Improvements

Make the primary interaction surface feel responsive and informative.

- **Enter to send**: Enter key submits the message. Shift+Enter inserts
  a newline. Add a user preference toggle in settings or localStorage.
- **Message timestamps**: show relative time ("2m ago") on each message.
  Show absolute time on hover (tooltip). Update periodically.
- **"Thinking" indicator**: animated dots or spinner shown between user
  message send and first assistant chunk. Disappears when streaming starts.
- **Conversation search**: search/filter input at the top of the
  conversation sidebar. Filters by title (client-side for loaded
  conversations).
- **Conversation pagination**: replace `listAll()` with cursor-based
  loading. Show "Load more" at the bottom of the sidebar. Initial load:
  50 conversations.
- **Cancel turn visibility**: show a more prominent "Agent is working..."
  indicator with the cancel button when a turn is in progress.

### 5. Keyboard Shortcuts

High-frequency actions only — small, documented set.

- **`/`**: focus chat composer (when not already focused).
- **`Cmd+N` / `Ctrl+N`**: new conversation.
- **`Escape`**: cancel current turn, close modal, or blur composer.
- **`?`**: show keyboard shortcut help overlay.
- **Implementation**: global keydown handler in the app layout. Shortcuts
  disabled when a text input/textarea is focused (except Escape).
- **Help overlay**: modal listing all shortcuts, triggered by `?` key
  or a help button in the header.

### 6. Visual Polish

Bring the UI from "functional" to "cohesive."

- **Icons**: add lucide-svelte icons to:
  - Navigation items (Chat, Agents, Tasks, Workflows, Settings)
  - Action buttons (New, Save, Delete, Test, Sync, Submit, Cancel)
  - Status badges (healthy, degraded, error)
  - Empty states
  - Toast notifications
- **Styled selects**: replace native `<select>` elements with styled
  dropdown components that match the design system (or apply consistent
  CSS to native selects).
- **Empty states**: replace plain text empty states with illustrations
  or icons and contextual guidance:
  - No conversations → "Start your first conversation" with New button
  - No agents → "Create an agent to get started" with link
  - No tasks → "Your task board is empty" with create draft guidance
  - No workflows (user) → "Duplicate a system workflow to customize it"
- **Back navigation**: add "Back to [list]" links on detail pages:
  task detail → task board, agent edit → agent list, workflow detail →
  workflow list.

### 7. Accessibility

Deferred from Stage 8 — implement the baseline.

- **ARIA labels**: add `aria-label` to all icon-only buttons, navigation
  links, status badges, and form controls without visible labels.
- **Skip-to-content link**: hidden link at the top of the page that
  becomes visible on focus, jumps to main content area.
- **Keyboard-navigable alternatives**: for drag-and-drop on the task
  board and workflow step editor, add move-up/move-down buttons that
  are visible on focus or via a menu.
- **Focus management**: when modals open, trap focus inside. When modals
  close, return focus to the trigger element.
- **Screen-reader pass**: test core flows (login, chat, agent creation,
  task board) with VoiceOver. Fix any announced-but-invisible or
  invisible-but-interactive elements.

### 8. Mobile and Responsive

Make the app usable on tablets and phones.

- **Hamburger menu**: on mobile widths (`< lg`), replace the hidden
  sidebar with a hamburger icon in the header that opens a slide-out
  drawer with navigation, user info, and sign-out.
- **Chat layout**: on mobile, conversation sidebar becomes a full-screen
  list. Selecting a conversation navigates to a full-screen chat view.
  Back button returns to the list.
- **Login page**: show branding text on mobile (currently
  `hidden lg:block`). Stack vertically instead of side-by-side.
- **Task board**: horizontal scroll on narrow screens. Column headers
  sticky. Or collapse to a single-column list view on mobile.
- **Forms**: full-width inputs on mobile. Multi-column grids collapse
  to single column (most already do via `md:grid-cols-*`).

## Acceptance Criteria

- [x] Toast notifications appear on all create/save/delete operations
- [x] All forms show required field indicators and inline validation
- [x] Destructive actions require confirmation dialog
- [x] Unsaved-changes warning on agent form, workflow editor, settings
- [x] Chat supports Enter-to-send with Shift+Enter for newlines
- [x] Messages show timestamps (relative, absolute on hover)
- [x] "Thinking" indicator shows during agent processing
- [x] Conversation sidebar has search and pagination
- [x] Keyboard shortcuts work: `/`, `Cmd+N`, `Escape`, `?`
- [x] Shortcut help overlay is accessible
- [x] Lucide icons appear in navigation, buttons, and empty states
- [x] Empty states show contextual guidance
- [x] Detail pages have back navigation links
- [x] ARIA labels on all interactive elements without visible labels
- [x] Skip-to-content link works
- [x] Drag-and-drop has keyboard alternatives
- [x] Core flows pass VoiceOver screen-reader test
- [x] App is navigable on mobile widths via hamburger menu
- [x] Chat works on mobile (list → detail navigation)
- [x] Login page renders correctly on mobile

## Key References

- `ui/src/routes/(app)/+layout.svelte` — app shell, navigation, sidebar
- `ui/src/routes/(app)/chat/[conversationId]/+page.svelte` — chat page
- `ui/src/lib/components/agents/AgentForm.svelte` — agent form
- `ui/src/routes/(app)/settings/+page.svelte` — settings page
- `ui/src/routes/(app)/tasks/+page.svelte` — task board
- `ui/src/routes/(app)/workflows/+page.svelte` — workflow editor
- `ui/src/routes/login/+page.svelte` — login page
- `ui/package.json` — lucide-svelte already in dependencies
- `docs/specs/09-ui-ux.md` — UX design spec

## Implementation Notes

- Shipped a shared toast system, confirmation dialog, shortcut help overlay,
  unsaved-change guards, skip link, mobile navigation drawer, and icon-rich
  app shell updates.
- Chat now supports search + cursor pagination for conversations, Enter to
  send, relative timestamps, a thinking indicator, stronger in-progress turn
  visibility, and mobile conversation switching.
- Agent, task, workflow, setup, login, and settings forms now provide stronger
  required-field handling, success/error feedback, and destructive-action
  confirmations.
- Keyboard move-up/move-down affordances were added to the task board alongside
  drag-and-drop.
