# Using Chat

The chat workspace is where you talk to an agent, watch responses stream in real time, and follow tool usage or delegated work without leaving the conversation.

## Starting a conversation

Open `Chat`, create a new conversation, and select an agent.
Web conversations can exist before they have an active session; Cognis creates the root session when you send the first real message.

That first message is also used to bootstrap Intaris intention generation right away, so the session purpose is available from the first turn instead of waiting for a follow-up turn.

## What the chat UI shows

Depending on what the agent is doing, the conversation can display:

- streaming assistant responses
- reasoning or progress blocks
- tool call indicators and results
- delegation status cards for background work
- queued-message indicators when you send another message during an active turn
- reconnection status for the WebSocket session

## Approvals and escalations

If Intaris escalates a tool call, the chat UI shows an approval prompt. From there you can approve or deny the requested action.

This is how Cognis keeps risky or sensitive actions visible to the user instead of silently executing them.

## Delegation and background work

Some work is better handled through delegated or structured execution instead of one immediate chat turn. When that happens, Cognis can show:

- a delegation card
- intermediate progress
- final completion or failure updates

The main conversation stays responsive while the sub-session or workflow continues.

When background work finishes, Cognis classifies the follow-up before the agent
responds:

- results that still belong to the active work thread can be integrated back
  into that thread naturally
- scheduled briefs, pauses, and unrelated completions are shown as separate
  updates instead of pretending an older chat topic is still active

## Session management and compaction

Long conversations may be compacted so the active context stays usable. When that happens, the timeline can show a compaction card and Cognis continues from the new active session with the compacted summary included in context.

## First-run readiness

Admins may see setup guidance until:

- Mnemory is reachable
- Intaris is reachable
- at least one LLM provider is configured
- at least one agent exists

## Recovery and diagnostics

If something looks wrong, check the system section for:

- provider health
- readiness diagnostics
- configuration summary
- database and key information

If the WebSocket connection drops, the UI will attempt to reconnect and recover missed events.

## Install as an app (PWA)

Cognis ships as a Progressive Web App. On desktop browsers (Chrome/Edge/Safari) and Android, you can install it for a dedicated window, app-icon launch, and an offline shell:

- **Chrome / Edge (any platform)**: a one-time install banner appears inside the app. You can also use the browser's address-bar install button, or menu -> "Install Cognis".
- **iOS Safari**: tap the Share icon, then "Add to Home Screen". Cognis detects this environment and shows a one-time hint with the instructions.
- **Android Chrome**: tap menu -> "Install app" or wait for the in-app install banner.

When installed:

- The app launches in a standalone window with no browser chrome, respecting the safe-area insets on iPhone notches.
- The app shell (HTML, JS, CSS) is cached so the UI loads even when the network is unavailable. Conversations still require a live WebSocket connection.
- Updates are applied automatically. When a new version is available, a small banner appears at the top of the screen with a "Reload" action.

## Mobile-specific behavior

- Primary navigation on mobile is a bottom tab bar (Chat / Tasks / Agents / Settings). Inside a chat conversation the bar hides so the composer owns the bottom safe-area.
- Tapping the hamburger button in the header opens a right-side sheet drawer with the full navigation. The sheet supports swipe-down-to-dismiss.
- The composer uses 16 px text on mobile so iOS Safari does not zoom the viewport when you focus it.
- The bottom tab bar, composer, and floating action bars respect the `safe-area-inset-bottom` so they sit above the Home Indicator on iPhones.
