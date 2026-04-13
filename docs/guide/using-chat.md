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
