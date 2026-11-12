# Evaluation size fitting and response diagnostics

## Request-size boundary

Clipping individual tool descriptions does not bound the final JSON object.
Unicode and JSON escaping can make individually valid fields exceed the
receiver's compact, ASCII-escaped context limit.

The final Intaris provider boundary fits only known explanatory description
fields, after the router adds the minimum outcome and approval receipt. It uses
the receiver's JSON size convention and visible middle-truncation markers.
Reductions are shared among descriptions; head/tail text is retained where space
permits.

Protected identities, source/operation descriptors, schema hashes, modes,
read-only requirements, policy/receipt fields, unknown context fields, and
complete action arguments are unchanged. Too-large protected context fails
closed before HTTP dispatch, reporting only numeric size/limit metadata.
Genuine action arguments are never shrunk to evade a server limit.

Ownership is pure per-call provider projection: no shared state, cache mutation,
I/O, lock, transaction or cancellation work. Existing dispatch/retry owners
remain. Fitting does not change the public schema or receiver size limit.

## Response diagnostics

Typed provider-response validation failures use content-free typed exceptions
with finite diagnostic codes. These codes distinguish known validation
boundaries without logging response bodies or weakening validation. An invalid
answer is not normalized, accepted, retried or redirected merely to hide an
error.

Latency below a configured timeout does not identify the cause of a response
failure. Attribute failures from their actual operation and diagnostic evidence,
not from an unrelated timeout setting.

## Verification

The regression tests cover native inference API rejection, typed adaptation,
final context fitting, protected outcome acknowledgements, router semantics,
Unicode, quotes and escapes, immutable arguments and protected overflow.

```sh
COGNIS_SKIP_UI_BUILD=1 make test PYTEST_ARGS='tests/unit/test_intaris_context_budget.py tests/unit/test_systemone_response_diagnostics.py tests/unit/test_inference_api.py tests/unit/test_guardrails_context.py tests/unit/test_intaris_evaluation.py tests/unit/test_tool_router.py -q --tb=short'
```

A receiver-schema cross-check must name the exact local schema inspected.
Passing that check does not establish deployed-schema equality, a live retry
outcome or broad provider coverage.
