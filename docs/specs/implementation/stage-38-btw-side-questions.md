# Stage 38: `/btw` Side Questions and Context Lanes

## Status

PLANNED

## Goal

Implement `/btw <question>` as a same-session side-question command that can
answer quick questions about the current conversation, including in-flight
mid-turn work, without interrupting the active main turn, entering the normal
queued-message pipeline, or polluting future main-turn context.

The implementation introduces context lanes in Intaris-backed session events so
side-question history is durable and follow-up-capable, but excluded from normal
main-agent prompts unless explicitly promoted later.

## Non-Goals

- No tool execution from `/btw` in this stage.
- No full side-thread promotion UI in this stage; only store metadata so it can
  be added later.
- No provider-specific advisor/server-tool integration.
- No new Cognis session, Intaris child session, or Mnemory session per `/btw`.
- No automatic inclusion of side-thread content in normal main turns.
- No raw active-stream/tool snapshot persistence beyond existing volatile
  scheduler snapshots.

## Core Invariants

These are hard rules. The implementation MUST NOT violate them.

1. **Same session, separate lane.** `/btw` writes durable records into the active
   Intaris session, but every `/btw` user/assistant record is marked
   `lane="side"` and `prompt_visibility="side_only"`.

2. **Main context stays clean.** Normal main-turn context assembly, compaction,
   memory extraction, title/intention updates, undo/redo, delivery, and queued
   message processing exclude side-lane messages by default.

3. **Side context is scoped.** `/btw` includes main-lane history plus only the
   matching side thread's side history. Other side threads are excluded unless an
   explicit inspector/promoter requests them.

4. **Mid-turn visibility without protocol corruption.** When anchored to an
   active main turn, `/btw` includes volatile active assistant stream and active
   tool-output snapshots as plain controller context blocks, not as provider
   protocol-level assistant/tool messages.

5. **No tools.** Initial `/btw` requests use `tool_policy="none"`. The controller
   executes no tool calls from side-question responses. If a provider returns
   tool calls anyway, they are rejected and recorded as diagnostics/notice.

6. **Cache reuse first.** `/btw` reuses the existing session/context assembly and
   prompt-cache breakpoint as much as possible. It must not create a new session
   or rebuild a bespoke full transcript when the normal cached context path can
   be used.

7. **Transport neutral core.** The command is implemented in the
   transport-agnostic command layer. WebSocket and channel-bound callers should
   receive the same command result semantics, with UI-specific rendering layered
   on top. Chat v2 REST remains the canonical timeline read/send contract and
   does not execute slash commands in this stage.

## Event Metadata

Historical events without lane metadata are treated as:

```json
{
  "lane": "main",
  "prompt_visibility": "main",
  "tool_policy": "normal"
}
```

`/btw` records normal message event types with metadata:

```json
{
  "type": "user_message",
  "data": {
    "content": "why are you running the full test suite?",
    "turn_id": "side_turn_01HV...",
    "lane": "side",
    "side_thread_id": "btw:turn_01HV...",
    "anchor_turn_id": "turn_01HV...",
    "prompt_visibility": "side_only",
    "tool_policy": "none",
    "command": "btw"
  }
}
```

The assistant answer uses the same `lane`, `side_thread_id`, `anchor_turn_id`,
`prompt_visibility`, and `tool_policy`. Store model/provider/usage fields on the
assistant side message when available.

## Side Thread Selection

Implement a deterministic helper, for example `resolve_side_thread(...)`:

1. If a client supplies an existing `side_thread_id`, validate that it belongs to
   the same conversation/session and use it.
2. Else if the conversation has an active main turn, use
   `side_thread_id="btw:<active_turn_id>"` and `anchor_turn_id=<active_turn_id>`.
3. Else if the client/UI carries an open side panel thread, use that thread.
4. Else anchor to the most recent completed main turn, using a new side thread
   ID if no existing post-turn side thread is selected.

The first implementation may support only cases 1, 2, and 4 from the API layer;
the UI can add richer open-panel selection later.

## Context Assembly

Add a lane-aware event selector used by all model-facing transcript builders:

```python
class ContextLaneSelector:
    def select_for_main(self, events: Sequence[SessionEvent]) -> list[SessionEvent]:
        ...

    def select_for_side(
        self,
        events: Sequence[SessionEvent],
        *,
        side_thread_id: str,
    ) -> list[SessionEvent]:
        ...
```

Rules:

- `select_for_main` includes events where `lane` is absent/`main` and
  `prompt_visibility` is absent/`main`.
- `select_for_main` excludes `lane="side"` and `prompt_visibility="side_only"`.
- `select_for_side` includes the same main events plus side events with the
  matching `side_thread_id`.
- `select_for_side` excludes other side threads.
- `prompt_visibility="excluded"` is never selected except by explicit diagnostic
  tooling.

`ContextAssembler` should expose a side-question assembly path or options object
rather than duplicating context construction. The `/btw` path should:

1. refresh/reuse the session cache normally;
2. use the existing immutable prefix and cache breakpoint;
3. apply side selection to recent events;
4. append a side-question instruction block;
5. append active-turn volatile context if available;
6. append the current side question as the final user message.

Suggested side-question instruction:

```text
This is a /btw side question from the user. Answer directly in one response.

Constraints:
- You have no tools.
- You cannot inspect files, run commands, search, or take actions.
- Use only the current conversation context, side-thread context, visible
  in-flight turn snapshots, and general knowledge.
- Do not promise follow-up work.
- If the answer is not available from the provided context, say so.
```

## Active Mid-Turn Context

Use existing `TurnScheduler` volatile snapshots:

- `active_stream_snapshots(conversation_id)`
- `active_tool_output_snapshots(conversation_id)`

Only include snapshots whose `turn_id` matches the side thread's
`anchor_turn_id`. Format them as bounded plain text/XML-ish context:

```text
<active_turn_context anchor_turn_id="turn_...">
  <assistant_stream status="running">
  ...
  </assistant_stream>

  <tool_output call_id="call_..." tool_name="bash" status="running" truncated="true">
  ...
  </tool_output>
</active_turn_context>
```

Do not write these snapshots into Intaris as part of `/btw`; they are volatile
context only. Existing active-tool-output L2 Redis persistence remains the source
for cross-process/reconnect visibility.

## LLM Invocation

Implement `/btw` as a one-shot non-streaming call initially:

- use current conversation/session model and provider overrides;
- call `LLMProvider.generate(...)` or equivalent side-question helper;
- pass the cache breakpoint returned by context assembly;
- start with `tools=[]` for safety;
- set a conservative max token budget;
- mark request/task type as `side_question` if provider accounting supports it.

If a future provider path can keep stable tool schemas without enabling tools,
it may use the normal tool array plus `tool_choice="none"`, but only with a
hard post-response guard that rejects any returned tool calls.

## Slash Command Behavior

Add `/btw` as a prefix command:

- `/btw` with no question returns usage.
- `/btw <question>` is allowed during active/busy turns.
- Normal busy-turn command rejection remains for mutating commands.
- The command does not call `TurnScheduler.submit_turn(...)`.
- The command records side-lane user and assistant events in Intaris and updates
  session cache consistently.
- The command returns:

```json
{
  "type": "side_question_answer",
  "conversation_id": "...",
  "session_id": "...",
  "side_thread_id": "...",
  "anchor_turn_id": "...",
  "question": "...",
  "answer": "...",
  "lane": "side",
  "tool_policy": "none",
  "usage": {},
  "tool_call_rejected": false
}
```

## API and UI

REST chat message routes were removed in favor of Chat v2. Side-question command
execution should use the WebSocket command path; canonical timeline reads should
use Chat v2 snapshot/sync/timeline endpoints.

WebSocket:

- existing command result delivery may be reused, but the result type must be
  distinguishable from plain `system_message`;
- optionally emit a dedicated `side_question_answer` frame after command
  execution for live UI updates.

UI:

- add autocomplete/help entry for `/btw <question>`;
- allow `/btw` during active turn while normal messages continue to queue;
- render side-lane messages in an associated side panel/card rather than inline
  main transcript;
- allow follow-up from the side panel by sending the existing `side_thread_id`;
- display no-tools rejection notices if `tool_call_rejected=true`.

## Intaris Compatibility

This stage intentionally avoids new Intaris event types. It relies on normal
`user_message`/`assistant_message` events with metadata fields in `data`.

Intaris changes still need care:

- event append/read paths must preserve lane metadata exactly;
- any Intaris-side reasoning/intention/summary logic that treats all messages as
  canonical conversation history must either ignore `lane="side"` by default or
  expose filtering controls for Cognis;
- search/audit views may show side-lane messages, but should label them as side
  context.

If current Intaris APIs cannot preserve/filter these fields safely, block this
stage until Intaris is updated. Do not emulate lanes only in Cognis cache while
persisting indistinguishable normal messages.

## Testing Plan

Unit tests:

- lane metadata defaulting treats historical events as main-lane;
- `select_for_main` excludes side-lane messages;
- `select_for_side` includes matching side thread and excludes unrelated side
  threads;
- `/btw` is accepted during `has_busy_turn=True`;
- `/btw` without a question returns usage;
- active stream/tool snapshots are included only when `turn_id` matches
  `anchor_turn_id`;
- provider-returned tool calls under `tool_policy="none"` are rejected and not
  executed.

Integration tests:

- WebSocket `/btw` records side-lane user/assistant messages and emits a
  distinguishable `side_question_answer` command result/frame;
- subsequent normal main turn does not include side-lane messages in prompt
  assembly;
- subsequent `/btw` in the same `side_thread_id` sees previous side messages;
- unrelated side thread does not see previous side messages;
- compaction input excludes raw side-lane messages;
- Chat v2 snapshot/sync/timeline projections preserve side metadata for UI
  rendering once the UI opts into side-lane views.

Regression tests:

- title/intention update logic ignores side turns;
- memory extraction ignores side turns unless explicitly enabled in a future
  policy;
- undo/redo operates only on main-lane normal turns;
- queued normal messages remain queued while `/btw` executes immediately.

## Recommended Implementation Order

1. Add lane metadata constants/helpers and a lane-aware selector.
2. Update context assembly and all model-facing history conversion to use the
   selector for main turns.
3. Add side-question context assembly that reuses normal cache/prefix handling.
4. Add `/btw` command dispatch, side-thread resolution, no-tools LLM call, and
   side-lane event persistence.
5. Extend WebSocket command result frames and Chat v2 timeline projection with
   lane metadata.
6. Add minimal UI support: autocomplete, side answer rendering, follow-up
   `side_thread_id`.
7. Add tests and update command help/docs.

## Open Questions

- Exact client payload shape for side-panel follow-ups (`side_thread_id` in
  message metadata vs a dedicated command payload).
- Whether side answers should be remembered by Mnemory behind an explicit
  per-agent policy later.
- Whether post-turn `/btw` should resume the most recent side thread by default
  or always create a new side thread unless the UI supplies one.
- Whether provider cache telemetry shows enough misses from `tools=[]` to justify
  implementing `tool_choice="none"` with stable schemas.
