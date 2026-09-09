# Signal send failures

Direct Signal sends preserve a small, sanitized failure projection from
signal-cli through the executor JSON-RPC boundary and the durable channel
outbox. The projection contains only a classification, provider error code,
validated non-negative `retry_after_seconds`, a boolean `challenge`, safe
`next_step` guidance, and `side_effect_certainty=uncertain`. Provider text,
recipients, and tokens are not forwarded or stored.

The projection is intentionally **not retryable**. signal-cli 0.14.5 reports
`RATE_LIMIT_FAILURE` as JSON-RPC `-5`, but its result can contain a message
that was accepted, and the token or delay does not prove nonacceptance. See
[SignalJsonRpcCommandHandler.java](https://github.com/AsamK/signal-cli/blob/v0.14.5/src/main/java/org/asamk/signal/jsonrpc/SignalJsonRpcCommandHandler.java),
[JsonSendMessageResult.java](https://github.com/AsamK/signal-cli/blob/v0.14.5/src/main/java/org/asamk/signal/json/JsonSendMessageResult.java),
[SendMessageResultUtils.java](https://github.com/AsamK/signal-cli/blob/v0.14.5/src/main/java/org/asamk/signal/util/SendMessageResultUtils.java),
and [issue #2071](https://github.com/AsamK/signal-cli/issues/2071).

Therefore `get_channel_delivery` exposes the actionable diagnostic and keeps
the public `last_error` value `external_send_outcome_uncertain` while the
delivery remains `uncertain`; no scheduled retry is added for this ambiguous
outcome. Existing durable scheduling/accounting remains the sole owner of
retry decisions. Unknown errors, timeouts, lost acknowledgements, and mixed
results remain uncertain as well.
