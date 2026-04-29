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

## Guardrails and approvals

Intaris evaluates tool calls, records session activity, and can escalate sensitive actions for human approval. Non-bypassable tools still go through guardrail checks even when an agent has broad tool permission.

Approvals are important for:

- destructive filesystem or shell actions
- credential use
- external communication
- browser actions that submit forms or mutate remote state
- skill or tool mutations

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
