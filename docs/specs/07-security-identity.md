# Cognis: Security and Identity

## Overview

Cognis is the identity authority for its agent control plane. It manages users,
agents, and service authentication. The controller is the sole gateway to
Mnemory and Intaris — executors never communicate with external services
directly.

## Authentication

### User Authentication

Users authenticate with Cognis to get a JWT:

```
POST /api/auth/login
  → 200 {token, expires_at, user}
```

MVP supports: username + password (**argon2id**), API key.
Future: OAuth 2.0 / OIDC, passkeys.

Password hashing: argon2id with `time_cost=3, memory_cost=65536 (64 MiB),
parallelism=4`. No bcrypt — argon2id is the modern standard.

### JWT Token Structure

User session token (used for Cognis API and UI):
```json
{
  "sub": "filip@pytloun.cz",
  "name": "Filip Pytloun",
  "role": "admin",
  "aud": ["cognis"],
  "iss": "cognis",
  "iat": 1711500000,
  "exp": 1711503600
}
```

The `sub` claim is the user's email — this is the user_id everywhere in the
ecosystem (Cognis DB, Mnemory X-User-Id, Intaris user_id).

### Service Authentication (Controller → Mnemory/Intaris)

The controller authenticates to Mnemory and Intaris using **JWT with
audience claims**. No API keys between services.

Service JWT (issued per-request or short-lived batch):
```json
{
  "sub": "filip@pytloun.cz",
  "agent_id": "aria",
  "aow": "owner@example.com",
  "aud": ["mnemory", "intaris"],
  "iss": "cognis",
  "exp": 1711503600
}
```

The `aud` (audience) claim prevents confused-deputy attacks. Each service
checks that its name is in the audience list.

The `aow` (agent-owner) claim is optional. It is present only when the
acting user (`sub`) is **not** the agent owner — i.e., a shared-agent
turn. When omitted, consumers MUST treat it as equal to `sub`. This
claim carries the `(user, owner)` distinction that Mnemory uses to
key memories and that Intaris will eventually use for owner-scoped
policies. See [28-agent-sharing.md](28-agent-sharing.md).

JWT validation is a Phase 0 prerequisite for both Mnemory (M1) and Intaris
(I5). Both services accept JWT alongside API keys for backward compatibility
with standalone usage.

**Key distribution:**
- **Local deployment**: Cognis writes public key to
  `~/.cognis/keys/public.pem`. Mnemory and Intaris read it via file path
  (`MNEMORY_JWT_PUBLIC_KEY`, `INTARIS_JWT_PUBLIC_KEY`).
- **Production deployment**: Cognis serves JWKS at
  `GET /.well-known/jwks.json`. Services validate via URL
  (`MNEMORY_JWKS_URL`, `INTARIS_JWKS_URL`).

**Executors do NOT receive service tokens.** They communicate only with the
controller via WebSocket. The controller proxies all Mnemory/Intaris calls.

### Executor Authentication

Executors authenticate to the controller with a short-lived token:
```json
{
  "sub": "executor",
  "executor_id": "exec_abc",
  "iss": "cognis",
  "exp": 1711500300
}
```

This token only authorizes WebSocket connection to the controller. The
executor has no credentials for any other service.

### JWT Key Management

Algorithm: **ES256** (ECDSA P-256).

Keys are auto-generated on first start and stored in `COGNIS_DATA_DIR/keys/`:
- `private.pem` — used by Cognis to sign JWTs
- `public.pem` — distributed to Mnemory/Intaris for validation

For production: provide your own keys via `COGNIS_JWT_PRIVATE_KEY_PATH`
and `COGNIS_JWT_PUBLIC_KEY_PATH` environment variables.

JWKS endpoint for remote validation:
```
GET /.well-known/jwks.json → {keys: [{kty, crv, kid, x, y}]}
```

Key rotation: include `kid` (key ID) in tokens. Serve multiple keys in JWKS.
Old keys remain for validation until all tokens expire.

### Mnemory/Intaris JWT Validation (Phase 0 Prerequisite)

Both services add JWT validation middleware (backward-compatible with API
keys):

```python
class CognisJWTValidator:
    """Accepts JWT or falls back to API key for backward compatibility."""

    def __init__(self, public_key_path: str | None, jwks_url: str | None):
        # Load from file (local) or JWKS URL (production)
        ...

    def validate(self, request) -> AuthContext:
        token = extract_bearer(request)
        try:
            claims = jwt.decode(token, self.public_key,
                                algorithms=["ES256"], issuer="cognis",
                                audience=self.expected_audience)
            return AuthContext(user_email=claims["sub"],
                               agent_id=claims.get("agent_id"))
        except jwt.InvalidTokenError:
            return self._validate_api_key(request)  # Fallback
```

### First-Start Bootstrap

On first start, if the `users` table is empty:

1. Controller detects no users exist.
2. Generates a one-time setup token (cryptographically random, URL-safe).
3. Prints to stdout:
   ```
   Cognis started on http://localhost:8080

   No users found. Complete setup at:
     http://localhost:8080/setup?token=<random_token>
   This link expires in 15 minutes.
   ```
4. `GET /setup?token=<token>` serves a setup page (create admin user:
   email, name, password).
5. `POST /api/setup` with the token creates the first admin user.
6. After the first user is created, the setup endpoint returns 404
   permanently. The token is invalidated.

**CLI fallback** (headless / missed the URL):
```bash
cognis-controller admin create-user admin@example.com --name "Admin"
# Prompts for password interactively
```

The `cognis-controller admin` commands access the database directly (not the API).
They require local filesystem access to the Cognis data directory.

**Container / CI seeding**:
```bash
COGNIS_INITIAL_ADMIN_EMAIL=admin@example.com \
COGNIS_INITIAL_ADMIN_PASSWORD=... \
uvx cognis-controller
```
Creates the admin user on startup if the users table is empty. Clears the
password from memory after use.

### Cross-Service UI Access

Cognis users can access Intaris and Mnemory UIs directly using their
Cognis JWT. Two supported patterns:

**Direct links with token exchange** (local / simple deployments):
1. Cognis UI provides "View in Intaris" / "View in Mnemory" links.
2. Cognis issues a short-lived, single-use exchange token:
   `POST /api/v1/auth/exchange-token` → `{token, target, expires_in}`
3. Link opens target UI with the exchange token as a query parameter.
4. Target service exchanges the token for a session (validates JWT
   signature, creates local session cookie).
5. Token is single-use and expires in 60 seconds.

**Reverse proxy / ingress** (production deployments):
```
cognis.example.com/          → Cognis UI + API
cognis.example.com/intaris/  → Intaris UI + API (proxied)
cognis.example.com/mnemory/  → Mnemory UI + API (proxied)
```
Single domain, shared JWT cookie. Handled by nginx, Traefik, or
Kubernetes Ingress — not by Cognis itself. This is an infrastructure
concern, documented in [11-deployment.md](11-deployment.md).

## Authorization

### User Roles

```python
class UserRole(str, Enum):
    ADMIN = "admin"      # System-level authority (see below)
    USER = "user"        # Own agents, own conversations
    VIEWER = "viewer"    # Read-only
    SERVICE = "service"  # API access, no UI
```

#### Admin authority

The `admin` role confers **system-level** authority, not peer-level
authority over other users' resources. Admins can:

- Manage users (`/api/v1/admin/users/*`, CLI `admin` commands).
- Configure LLM providers, model routing, executors (global), system
  settings, MCP servers, and skills shipped with the install.
- Manage system agents and their overrides.
- Read system-level audit log and reconcile endpoints.
- Approve break-glass recovery via direct DB access (CLI).

Admins **cannot** — purely on the basis of their role — read or act
on another user's:

- Agents, agent configuration, or agent bindings
- Conversations, sessions, tasks, schedules
- Personal memories (Mnemory records with `user == the other user`)
- Secrets
- API keys

An admin may gain access to another user's agent only through the
same mechanism as any other user: an explicit `agent_grants` share
by that agent's owner. This rule is called out explicitly because
many SaaS products ship a super-admin bypass; Cognis does not. See
[28-agent-sharing.md](28-agent-sharing.md) for the full access
matrix.

Break-glass (legitimate cross-user operations for support or legal
response) is performed via direct DB / CLI access by an operator,
which is traceable and distinct from an in-app role.

### User Management

Admins can manage users via the REST API (`/api/v1/admin/users/*`) or CLI
(`cognis-controller admin` commands). The Users table includes:

- `is_active` — soft delete flag; disabled users cannot log in or use API
- `last_login_at` — updated on each successful login
- `disabled_at` / `disabled_by` — audit trail for soft deletes

**Soft delete (disable):** Sets `is_active=false`. The user's data remains
intact. The middleware checks `is_active` on every authenticated request
(JWT, API key, cookie) and returns `403 Account disabled` for inactive users.

**Hard delete:** Cascades to all user-owned resources in Cognis DB
(conversations, sessions, agents, tasks, workflows, schedules, secrets,
API keys, executors, skills). Does NOT cascade to Mnemory/Intaris.

**Safety guards:** Cannot delete/disable yourself. Cannot demote, disable,
or delete the last admin user.

**Self-service:** Users can update their own display name via
`PATCH /api/auth/me`. Password changes use the existing
`POST /api/auth/change-password` endpoint.

### Resource Authorization

All data is scoped by the authenticated user's email.

```sql
SELECT * FROM conversations WHERE user_email = ? AND conversation_id = ?;
```

Agent-scoped routes use `check_agent_access(required=...)` rather
than a raw `owner_email == caller` filter. Access is granted by:

- ownership (`agent.owner_email == caller`), or
- an active `use` grant for the caller in `agent_grants`.

Admin role is **not** a third path. See the *Admin authority*
subsection above and [28-agent-sharing.md](28-agent-sharing.md) for
the full matrix.

The runtime carries two identities per turn:

- **acting user** (JWT `sub`) — who triggered the turn
- **agent owner** (`agent.owner_email`, propagated via `aow` claim
  and `X-Agent-Owner` header when different from `sub`)

Mnemory keys records by both dimensions, so:

- the agent's identity/core memories always resolve to the owner's
  namespace regardless of caller,
- the caller's episodic memory on the shared agent is scoped to the
  caller,
- no caller's personal memory outside the agent context leaks into
  the agent's namespace, and
- the owner's personal memory never leaks to grantees.

The controller enforces this by setting `X-Agent-Id` and
`X-Agent-Owner` on every Mnemory/Intaris call and by emitting the
optional `aow` claim when it differs from `sub`.

## Multi-Tenancy

Since the controller is the sole client for Mnemory/Intaris:
- User identity flows from the authenticated JWT through the controller
- Controller sets correct headers on all service calls
- No cross-tenant data access is possible
- Executors have no direct service access

## Secrets Management

### Scope Hierarchy

Resolution order (most specific wins):
1. Agent-scoped: `WHERE user_email=? AND agent_id=? AND name=?`
2. User-scoped: `WHERE user_email=? AND agent_id IS NULL AND name=?`
3. Global: `WHERE scope='global' AND name=?`

### Encrypted Storage

```python
class EncryptedDBSecrets(SecretsProvider):
    """AES-256-GCM encrypted secrets in DB."""

    def _encrypt(self, plaintext: str) -> bytes:
        nonce = os.urandom(12)
        cipher = AESGCM(self.key)
        return nonce + cipher.encrypt(nonce, plaintext.encode(), None)

    def _decrypt(self, data: bytes) -> str:
        nonce, ciphertext = data[:12], data[12:]
        return AESGCM(self.key).decrypt(nonce, ciphertext, None).decode()
```

### Secret Injection

Secrets are resolved by the controller at executor spawn time:

```python
async def resolve_for_execution(self, agent, user_email) -> dict[str, str]:
    """Resolve secrets for agent's allowed_secrets list."""
    resolved = {}
    for name in agent.permissions.allowed_secrets:
        resolved[name] = await self.get_secret(name, user_email, agent.agent_id)
    return resolved
```

Passed in `ExecutorConfig.secrets` for MCP server authentication. The
executor uses them only for local MCP connections — never for Mnemory/Intaris.

**For remote executors (Docker/K8s)**: secrets are in ExecutorConfig which
travels over the WebSocket. For production, consider using pull-based secret
injection (executor pulls from Vault/KMS using a short-lived token) instead
of push. This is a Phase 2 concern.

### Secret References in Config

```yaml
mcp_servers:
  - name: "github"
    env:
      GITHUB_TOKEN: "${secret:github_token}"  # Resolved at spawn time
```

## Non-Bypassable Safety

Certain tool categories always go through Intaris regardless of agent
permissions:

```yaml
security:
  non_bypassable_tools:
    - "shell"
    - "bash"
    - "write_file"
    - "delete_file"
    - "*/create_*"
    - "*/delete_*"
```

See [06-tool-system.md](06-tool-system.md) for details.

## Agent Identity

### Current (MVP)

Agents identified by `agent_id` (string slug), scoped per user. Used in:
- Mnemory: `X-Agent-Id` header for memory scoping
- Intaris: `X-Agent-Id` header for session tracking
- JWT claims for service tokens

### Future: Cryptographic Identity (Phase 3)

```python
class AgentIdentity(BaseModel):
    agent_id: str
    did: str | None = None           # did:web:cognis.example.com:agents:aria
    public_key: str | None = None
    key_algorithm: str = "Ed25519"
    credentials: list[VerifiableCredential] = []
```

See [08-federation.md](08-federation.md).

## API Security

- **Rate limiting**: per user and per agent
- **Input validation**: Pydantic models, max string lengths
- **CORS**: configured allowlist
- **WebSocket auth**: first-message authentication (not query params)
- **Login rate limiting**: prevent brute force

## Audit Trail

Two complementary audit mechanisms:

1. **Cognis audit_log table**: system-level events (auth, agent CRUD, config)
2. **Intaris event store**: session-level events (messages, tool calls, evaluations)

| Event | Where |
|-------|-------|
| User login/logout | Cognis audit_log |
| Agent create/update/delete | Cognis audit_log |
| Secret access (name only) | Cognis audit_log |
| User message | Intaris events |
| Assistant response | Intaris events |
| Tool call + evaluation | Intaris events |
| Delegation lifecycle | Intaris events |
| Compaction | Intaris events |

## Threat Model

| Threat | Mitigation |
|--------|-----------|
| User impersonation | JWT with short TTL, ES256 |
| Cross-tenant access | User-scoped queries, controller-mediated service calls |
| Secret exfiltration | Encrypted at rest, executor gets only allowed secrets |
| Prompt injection via tools | Intaris evaluates, non-bypassable for destructive tools |
| Executor escape | Containerized execution (Phase 2), no direct service access |
| Token replay | Short TTL, audience binding |
| Malicious agent behavior | Intaris behavioral analysis |
| Controller compromise | Single point — standard server hardening applies |
