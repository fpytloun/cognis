# Chat inspector Usage

The inspector has Overview, Work, Session and Usage tabs. Session remains the
place for current-session diagnostics. Usage identifies the inspected conversation
or session and offers a direct / including-children selection.

Conversation accounting means the whole retained conversation history: its own
root executions, including earlier reset, renew and undo history and compaction
rotations. Direct accounting excludes delegated, hook and managed-child work.
Including children adds authorized descendants of that same population.

An explicitly inspected session selects its execution's validated continuation
lane, not its containing conversation or siblings. A managed child opened as a
conversation selects that child conversation's retained history. Unsupported task
steps, missing identity and insufficient lineage must remain unavailable; they
must not fall back to a broader conversation.

## Accounting display

The tab reads the authenticated accounting summary API on demand using historical
pricing. Unsupported scopes, unavailable lineage and missing accounting remain
explicitly unavailable; no parent or broader query is substituted.

The panel preserves decimal monetary strings and separates every currency and
pricing basis. A known priced subtotal is not complete spend. Unknown token and
attempt values remain unknown, while reported zero remains zero. Token categories
may overlap and are not summed.

Reads are limited to the active, visible panel. Opening, target/inclusion changes,
returning to a visible document, reconnect and matching scope invalidation refresh
the selected target. Closing or hiding cancels pending reads; old responses cannot
replace the new selection. There is no polling or persistent accounting selection.
Fresh online sign-in is required. Logout and actor changes clear old results and
cancel pending reads. The backend revalidates current authorization on each read.

**Represented accounting sessions** counts distinct authorized physical sessions
represented in returned accounting, not all historical executions. Missing or
unavailable counts remain unknown. No observations do not prove zero usage.

Lineage, membership, authorization and deduplication are server-owned. No totals
are calculated from chat messages, active-session pointers, or subtraction of
separately fetched direct and inclusive snapshots.
