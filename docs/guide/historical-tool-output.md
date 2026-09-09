# Historical tool content

`read_conversation_messages` returns user and assistant messages by default.
The optional `kinds` filter also accepts `tool_call` and `tool_result`.
These event kinds return compact metadata only: call ID, available tool name, event kind, session, sequence, reference, and available error state.
They never include arguments, results, or previews.

Pass the returned `reference` and `call_id` to `read_tool_output`.
Use `part="arguments"` with a `tool_call` reference.
Use `part="result"` with a `tool_result` reference.
The detail reader verifies ownership, session membership, sequence, kind, and call ID before output-store access.
References are locators, not access grants.

`offset` is a one-based line number. `limit` defaults to 200 lines.
Historical reads clamp `limit` to 1–2000 lines and apply the current context-pressure character limit.
The returned `next_offset` points to the next unconsumed line.
A single oversized line can be truncated, with an explicit notice.
Existing output-store line truncation still applies.

Arguments come from the persisted event, not the original exact request.
Persisted arguments can contain a bounded or redacted representation.
Object arguments use formatted JSON. JSON-string arguments retain their stored text.
Result reads use the verified recovery handle when full stored output is available.
Otherwise, the reader labels the persisted event preview or reports unavailable content.
A missing store entry can be missing or expired. The reader does not claim a known expiry.

Message-only cursors from earlier versions remain supported.
New cursors bind the conversation and event-kind filter.
The `around` anchor uses a sequence-number window, not a count of matching events.
Known history gaps appear in `history_gaps`; filtered sequence gaps alone do not prove retention loss.
Legacy output search and anchor tools remain unchanged.
