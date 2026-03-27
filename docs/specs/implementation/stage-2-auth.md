# Stage 2: Auth + Bootstrap + CLI

**Status**: IMPLEMENTED*
**Repo**: `cognis`
**Depends on**: Stage 1
**Estimated effort**: 2-3 days

## Objective

Implement the full authentication system, first-start bootstrap flow, and
Typer CLI admin commands. After this stage, a user can create the first
admin account (via setup URL or CLI), log in, get a JWT, and make
authenticated API calls. The JWKS endpoint works so Mnemory and Intaris
can validate Cognis-issued tokens.

## Progress Notes

- Stage 2 auth/bootstrap/CLI implementation is complete.
- Implemented: ES256 JWT auth provider, refresh/service/exchange tokens,
  JWKS endpoint, setup flow, login/refresh/logout/me endpoints, HTTP auth
  middleware, API-key authentication, minimal WebSocket auth flow, FastAPI app
  factory, and Typer CLI commands.
- MVP note: token revocation and exchange-token single-use state are in-memory
  only and reset on process restart.
- Local validation passed through unit tests, `pytest`, `ruff`, and `mypy`.
- Full long-running server/runtime and WebSocket timeout verification remains
  environment-dependent.

## Deliverables

### 1. JWT Implementation

- `cognis/providers/auth/jwt.py`
  - Sign JWTs with ES256 (auto-generated private key)
  - Verify JWTs with public key
  - User tokens: `sub` = email, `aud` = `["cognis"]`, `role`, `name`
  - Service tokens: `sub` = email, `aud` = `["mnemory", "intaris"]`,
    `agent_id`
  - Configurable TTL (default 1h from `security.token_ttl_seconds`)
  - Refresh token support

### 2. JWKS Endpoint

- `GET /.well-known/jwks.json` — serves the public key in JWKS format
- Include `kid` (key ID) for future key rotation
- No auth required (public endpoint)

### 3. Auth Middleware

- `cognis/api/middleware.py`
  - JWT validation on all `/api/v1/` routes
  - API key validation (hash lookup in `api_keys` table)
  - Extract user identity into request state
  - Skip auth for: health, metrics, JWKS, setup endpoint
  - WebSocket auth: first message `{type: "auth", token: "..."}`
  - `ws_auth_timeout_seconds`: close unauthenticated connections after 10s

### 4. Auth API Routes

- `cognis/api/routes/auth.py`
  - `POST /api/auth/login` — email + password → JWT
  - `POST /api/auth/refresh` — refresh token → new JWT
  - `POST /api/auth/logout` — invalidate token
  - `GET /api/auth/me` — current user info
  - `POST /api/v1/auth/exchange-token` — short-lived token for
    Intaris/Mnemory UI access (60s TTL, single-use)

### 5. First-Start Bootstrap

- `POST /api/setup` — token-gated first admin creation
  - Only available when `users` table is empty
  - Requires one-time token (printed to stdout, 15 min TTL)
  - Creates admin user (email, name, password)
  - Returns 404 permanently after first use
- Container/CI: `COGNIS_INITIAL_ADMIN_EMAIL` + `COGNIS_INITIAL_ADMIN_PASSWORD`
  env vars create admin on startup if no users exist

### 6. CLI Admin Commands

- `cognis/cli/admin.py` (Typer subcommand group)
  - `cognis admin create-user <email> --name "Name"` — prompts for password
  - `cognis admin reset-password <email>` — prompts for new password
  - `cognis admin api-key create <email> --name "key-name"` — generates key,
    prints it once
  - `cognis admin api-key list <email>` — list keys (metadata only)
- `cognis/cli/serve.py` — start the FastAPI server
- `cognis config init` — print env var template to stdout
- `cognis status` — call `/api/health` and display results
- All admin commands access the DB directly (no API auth needed)
- Password hashing: argon2id

### 7. FastAPI Application

- `cognis/api/app.py` — FastAPI factory with:
  - Auth middleware
  - CORS middleware
  - Health endpoint (`/api/health`)
  - JWKS endpoint
  - Setup endpoint
  - Auth routes
  - Lifespan: startup (check for first-start, print setup URL) and
    shutdown hooks

## Acceptance Criteria

- [ ] `cognis serve` starts FastAPI on configured host/port
- [ ] First start prints one-time setup URL with 15 min TTL
- [ ] `POST /api/setup` creates first admin user (with valid token)
- [ ] `POST /api/setup` returns 404 after first user exists
- [ ] `cognis admin create-user` creates user directly in DB
- [ ] `POST /api/auth/login` returns JWT for valid credentials
- [ ] JWT contains correct `sub`, `aud`, `role` claims
- [ ] `GET /.well-known/jwks.json` returns valid JWKS
- [ ] Auth middleware rejects requests without valid JWT or API key
- [ ] `cognis admin api-key create` generates working API key
- [ ] `COGNIS_INITIAL_ADMIN_EMAIL` + `COGNIS_INITIAL_ADMIN_PASSWORD` seeds admin
- [ ] Token exchange endpoint returns short-lived token
- [ ] WebSocket rejects connections that don't auth within 10s
- [ ] Unit tests for JWT sign/verify, auth middleware, password hashing
- [ ] `ruff check` and `mypy` clean

## Key References

- `docs/specs/07-security-identity.md` — JWT, bootstrap, cross-service access
- `docs/specs/10-api-spec.md` — auth endpoints, JWKS
- `docs/specs/09-ui-ux.md` — CLI commands
