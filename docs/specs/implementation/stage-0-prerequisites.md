# Stage 0: Prerequisites

**Status**: IN PROGRESS
**Repos**: `intaris`, `mnemory`, `cognis` (contract tests only)
**Depends on**: nothing
**Estimated effort**: 1-2 weeks

## Objective

Prepare Intaris and Mnemory for Cognis integration. Add JWT validation to
both services so Cognis can authenticate using signed tokens instead of API
keys. Extend Intaris event APIs with features Cognis needs for the session
cache. Write contract tests in the Cognis repo that validate the exact API
shapes Cognis expects.

## Intaris Changes

### I1: Extend VALID_EVENT_TYPES

Add event types that Cognis will record:

- `user_message`
- `assistant_message`
- `delegation`
- `compaction_summary`

These must be accepted by `POST /session/{id}/events` without validation
errors.

### I2: UI Formatting For New Event Types

Review the Intaris UI session recording player to ensure the new event types
render sensibly. At minimum, they should not cause errors or blank entries.

### I3: Reverse Read / last_n

Add `last_n` parameter to `GET /session/{id}/events`. When set, return the
last N events in chronological order. This must work efficiently with the
S3/filesystem backend (read the last chunk, not all chunks).

### I4: last_seq Endpoint

Expose the last event sequence number via the API. Either:
- Add `last_seq` field to `GET /session/{id}/events` response (even empty)
- Or add a dedicated lightweight endpoint

### I5: JWT Validation Middleware

Add ES256 JWT validation alongside existing API key auth:

- Accept `Authorization: Bearer <jwt>` with ES256 signature verification
- Extract authenticated user email from JWT `sub` claim
- Extract `agent_id` from JWT `agent_id` claim (optional)
- Verify `aud` includes `"intaris"`
- Key source: file path (`INTARIS_JWT_PUBLIC_KEY`) or JWKS URL
  (`INTARIS_JWKS_URL`)
- Backward compatible: if JWT validation fails, fall back to API key auth

### I6: Event Recording Idempotency

Add optional `idempotency_key` query parameter to the event append endpoint.
Format: `{session_id}:{turn_number}:{batch_index}`. If a duplicate key is
received, return success without re-appending. Prevents duplicate events
when the controller retries after a timeout.

## Mnemory Changes

### M1: JWT Validation Middleware

Same pattern as Intaris I5:

- Accept `Authorization: Bearer <jwt>` with ES256 signature verification
- Extract authenticated user email from JWT `sub` claim (maps to `X-User-Id`)
- Extract `agent_id` from JWT `agent_id` claim or `X-Agent-Id` header
- Verify `aud` includes `"mnemory"`
- Key source: file path (`MNEMORY_JWT_PUBLIC_KEY`) or JWKS URL
  (`MNEMORY_JWKS_URL`)
- Backward compatible: fall back to API key auth

## Cognis Contract Tests

Write contract tests in `cognis/tests/contract/` that validate:

- Mnemory recall/remember API shapes (fields, types, behavior)
- Intaris evaluate/reasoning/events API shapes
- Intaris event recording with idempotency key
- JWT authentication to both services
- Error responses and status codes

Tests run against live services (not mocks). They should be the first code
in the Cognis repo.

## Acceptance Criteria

- [ ] All 6 Intaris changes (I1-I6) merged and released
- [ ] Mnemory M1 merged and released
- [ ] `uvx mnemory` and `uvx intaris` accept Cognis-issued JWTs
- [ ] Contract tests in `cognis/tests/contract/` pass against live services
- [ ] Backward compatibility verified (API key auth still works)

## Key References

- `docs/specs/05-integrations.md` — verified API contracts
- `docs/specs/07-security-identity.md` — JWT structure and key distribution
- `docs/specs/12-mvp-roadmap.md` — prerequisite tables
