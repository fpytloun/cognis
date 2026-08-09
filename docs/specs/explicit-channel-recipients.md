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

Resolution intents are durable and account-owned. They bind the normalized
recipient, account, payload fingerprint, and ordered authorized artifacts.
Resolution occurs outside the database transaction through a generation-fenced
adapter view. Account ownership, enablement, capability gates, and active
managed-route binding are rechecked before an outbox row is created and before
provider send. Managed bindings always win and block the explicit send.
