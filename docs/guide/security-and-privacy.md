# Security and Privacy

Cognis is designed around explicit boundaries: the controller decides, executors do, Mnemory stores memory, and Intaris records/evaluates session activity. This separation makes security easier to reason about, but the final privacy properties still depend on how you configure model providers, tools, MCP servers, channels, and executors.

## Data ownership

| Data | Owner | Notes |
|---|---|---|
| Users, agents, projects, tasks, workflows, settings | Cognis | Stored in the Cognis database |
| Secrets and credentials metadata | Cognis | Encrypted at rest when secret values are stored |
| Session content and tool-call audit | Intaris | Durable conversation/session event store |
| Long-term memories | Mnemory | Recall and remember data |
| Tool runtime state | Executor | Browser profiles, workspaces, caches, local files |

Cognis does not intentionally duplicate Mnemory or Intaris durable state into its own database.

## Secrets and credential references

Secrets are encrypted at rest using the Cognis secrets key. Credential and secret values are not placed directly into LLM prompts.

Instead, Cognis uses reference-style values such as `value_ref` identifiers. The model can see that a credential reference exists when a workflow needs it, but not the raw value. The executor resolves the reference only when an authorized tool actually needs the value at execution time.

This gives agents enough information to complete a task without leaking API keys, passwords, browser storage state, or token values into prompt text.

## Password login and TOTP MFA

The `security.mfa_policy` system setting accepts `optional` or `required`.
The default is `optional`.

- With `optional`, users without a factor can sign in with a password.
- Enrolled users must always supply a TOTP or one-time recovery code.
- With `required`, the first password login forces TOTP enrollment before Cognis creates a session.

The policy applies to the next password login. Existing browser and native
sessions remain valid. A normal refresh of an existing session does not become
a new MFA prompt.

Cognis supports standard 6-digit, 30-second, SHA-1 TOTP authenticator apps.
The enrollment QR code is rendered locally. Save the recovery codes when Cognis
shows them because each code is shown once and works once.

An optional-policy user can disable MFA from Account Settings. Disabling MFA or
regenerating recovery codes requires the current password and a current TOTP or
recovery code. Required-policy users cannot disable MFA.

If a user loses all factors, run this local command on the controller host:

```bash
cognis-controller admin reset-mfa user@example.com
```

This command removes the factor and revokes the user's browser and native
sessions. Under the required policy, the next password login forces enrollment.
There is no remote administrator bypass.

WebAuthn and passkeys are intentionally deferred. The challenge responses
include a method discriminator so a future release can add another method
without changing the password-login contract.

## What the LLM can still see

The LLM receives the prompt, user-visible conversation context, tool schemas, selected memory/context, and tool results that Cognis intentionally exposes for the turn. It should not receive raw secrets, but it may receive content returned by tools, web pages, files, or MCP servers.

Privacy therefore depends on:

- which inference provider you choose
- whether inference runs from the controller or through an executor
- what memories and conversation history are relevant to the turn
- which tools and MCP servers are enabled
- what external web pages, files, or APIs return

If you need stronger privacy, use a trusted self-hosted or local inference provider and keep sensitive tools on executors you control.

## Controller and executor boundary

The controller owns orchestration and policy. Executors run tools. This matters because tool execution often touches sensitive local resources: files, browsers, credentials, shells, private networks, and MCP servers.

Use remote WebSocket executors when:

- tools need to run near a private network
- browser identity should stay on a user-controlled machine
- you want cloud-hosted orchestration without cloud-hosted tool state
- you want disposable cloud workers for stateless tasks

For shared deployments, disable in-process and subprocess executors and use only WebSocket executors.

The official controller image and a plain `pip install cognis-controller` do not
contain the executor package. They cannot execute host tools, and persisted
local executor rows remain unavailable until a compatible
`cognis-executor[full]` process is installed and connected. Treat a sidecar or
external executor as the security boundary that owns shells, files, browsers,
credentials, and local network access.

## Guardrails and approvals

When the Intaris guardrails backend is selected, Intaris evaluates tool calls,
records session activity, and can escalate sensitive actions for human approval.
Broad tool permission is not a substitute for guardrail evaluation.

Approvals are important for:

- destructive filesystem or shell actions
- credential use
- external communication
- browser actions that submit forms or mutate remote state
- skill or tool mutations

### Guardrails, analytics, and cost

The agent's guardrails backend and **Session analytics** toggle are independent:
Intaris/On enables preventive evaluation and retrospective monitoring;
Intaris/Off keeps preventive evaluation without analytics; None/On keeps
retrospective monitoring without ordinary preventive evaluation; None/Off disables
both kinds of optional Intaris LLM work on the normal Cognis path. Neither switch
disables durable event recording or tool audit.

It helps to distinguish three levels:

- **L1: recording and per-action evaluation.** Recording preserves the session's
  activity and audit trail. Evaluation is a separate policy operation, potentially
  involving inference. With Intaris guardrails on, turning analytics off does not
  turn evaluation off.
- **L2: session-window analysis.** Analytics summarize and assess windows within a
  session, including its compaction lifecycle. Repeated windows can process
  substantial context over a long session. These are retrospective operations,
  not permission checks for the action currently executing.
- **L3: cross-session behavioral profiles.** Analytics aggregate evidence across
  sessions to identify recurring behavior. This provides monitoring context,
  not a preventive authorization decision.

Evaluation cost follows evaluation activity; L2 cost follows eligible session
windows and compaction, and L3 cost follows behavioral-profile analysis cadence.
Actual tokens and cost depend on the configured models, context sizes, and
service scheduling. Enabling analytics is not a single fixed-cost request.
Disabling it prevents subsequent eligible analytics work without deleting existing
history or earlier results. Sessions use their effective analytics eligibility;
changing a toggle is not retroactive erasure.

With **None** and analytics **Off**, the normal Cognis path makes no Intaris
guardrail/analytics LLM calls. Storage and audit still operate. Cognis's own
conversation titles use `auxiliary_generation` separately; agent inference,
other auxiliary jobs, and optional vector-search embeddings can also have costs.
Do not interpret this combination as zero total inference or infrastructure cost.

Keep both controls on as the usual baseline for personal assistants and
autonomous/public-facing agents exposed to untrusted inputs. For isolated coding
agents, analytics can be off, and None may be appropriate when isolation and
scoped capabilities adequately contain the risk. Trusted lower-risk automation
can keep guardrails on while choosing analytics independently. Delegation inherits
the parent's effective configuration; managed conversations use the target's
configuration even for a same-agent target. Defaults remain unchanged, and Cognis
does not automatically toggle existing agents.

**None is not an authorization bypass.** Explicit mandatory Cognis constraints
and mandatory evaluation remain in force. Calling Intaris directly, including
`evaluate`, does not grant tool access or override Cognis authorization, ownership,
or approval requirements.

## Channel pairing

External channel senders should be verified before they can talk to an agent. Cognis supports pairing flows where a remote sender receives a short-lived code and redeems it in the UI.

Use pairing for public or semi-public channel accounts so unknown senders cannot immediately drive agent behavior.

## Browser privacy

Browser automation runs on executors. Persistent browser profiles can contain cookies, local storage, and site identity. Treat executor homes that store browser profiles as sensitive.

Use ephemeral browser sessions for throwaway browsing. Use persistent local profiles when you need continuity or when a site blocks fresh automated contexts.

## Operational recommendations

- Use `wss://` for remote executors.
- Keep executor tokens secret and rotate them if exposed.
- Disable local executor modes in shared deployments.
- Keep `COGNIS_SECRETS_KEY_PATH` backed up and access-restricted.
- Limit executor tool groups to what agents actually need.
- Review MCP servers before attaching them to executors or agents.
- Prefer pairing for external channel senders.
- Choose inference providers according to the sensitivity of your data.

## Privacy disclaimer

Cognis can avoid sending raw secrets to LLMs and can keep tool execution on your own executors, but it cannot make an external inference provider private. If prompts, memories, tool results, or web/file content are sent to a third-party model API, that provider's data handling policy applies.
