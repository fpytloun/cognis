# Explicit channel recipients

Outbound channel tools accept exactly one of:

- an existing opaque `target_ref`; or
- a `recipient` containing a channel type, normalized address, optional account
  reference, address kind, chat kind, and explicit resolution/creation gates.

Recipient addresses are validated centrally. Control characters, URI-like
values, malformed provider IDs, and address/chat-kind mismatches are rejected.
Raw provider addresses and chat IDs are not returned in tool results or errors.

Contactable target records represent routes that Cognis observed inbound or
successfully contacted outbound. An admitted recipient and a zero-receipt
delivery create no target record. The first durable outbound chunk receipt
promotes the resolved route transactionally. Promotion preserves an existing
display name and converges with concurrent inbound observation. Delivery
inspection returns an opaque `target_ref` only after that promotion.

An idempotency key identifies one exact one-shot payload. Reuse the same key
after an interrupted one-shot result or uncertain one-shot send. Cognis returns
the existing delivery without another provider send. A different payload with
the same key fails. A new key can create a duplicate when the provider outcome
is uncertain.

Resolution intents are durable and account-owned. They bind the normalized
recipient, account, payload fingerprint, and ordered authorized artifacts.
Resolution occurs outside the database transaction through a generation-fenced
adapter view. Account ownership, enablement, capability gates, and active
managed-route binding are rechecked before an outbox row is created and before
provider send. Managed bindings always win and block the explicit send.

Failed or uncertain managed deliveries keep exclusive route ownership until
the configured expiry boundary. Cognis does not retry uncertain deliveries.
At expiry, maintenance releases the route and preserves the delivery evidence.
It does not replay held participant messages.

A one-shot idempotency key cannot deduplicate an uncertain managed final.
Reconcile the managed outcome externally before any resend.

An authorized agent can call `agent_conversation_recover_channel` after expiry.
The action requires the current owner epoch and an audit reason. It only
releases the route. It does not transfer ownership, retry delivery, or replay
held messages.

An uncertain one-shot delivery also keeps its route reserved. A conflicting
managed-channel creation returns `blocker_type`, `blocker_id`, `blocker_status`,
and `recovery_tool`. First reconcile the provider outcome outside Cognis. Then
call `agent_conversation_recover_channel` with that `delivery_id` and an audit
reason. Do not include `conversation_id` or `expected_owner_epoch` for this
form. The release keeps the delivery status `uncertain`, its original error,
attempt count, source ID, and idempotency protection. It never sends the
delivery again. Repeating the release returns the stored audit result.

`route_reserved` reports aggregate occupancy at the locked transaction snapshot.
It does not describe only the selected delivery. `remaining_blockers` identifies
a managed binding and the next blocking one-shot delivery, when present.
A repeated release can report a new managed binding without changing its ownership.

Managed-channel creation adds `agent_conversation_send_controller` and
`agent_conversation_complete` automatically. These fixed controls can appear
in `allowed_tools`. No other tool bypasses normal availability and
authorization checks. `expires_at` must be timezone-aware, in the future, and
no more than 30 days away.
