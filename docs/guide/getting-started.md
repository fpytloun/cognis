# Getting Started

The shortest path is **deploy → account → models → executor → XO → first chat**.
The XO can then operate Cognis and create other agents. Credentials,
permissions, and consequential actions can still need human approval.

## 1. Deploy the components

Use [Deployment](deployment.md) for production or [Local Compose](local-compose.md)
for Cognis, Mnemory, Intaris, Qdrant, and an executor. The controller includes the
UI but cannot execute host tools.

From the repository, start a fresh interactive local installation:

```bash
cp .env.local.example .env.local
docker compose --env-file .env.local -f compose.local.yml up -d --build --wait cognis mnemory intaris qdrant
docker compose --env-file .env.local -f compose.local.yml logs cognis
```

Open the one-time setup URL from the logs and create the first admin account.
Leave both `COGNIS_LOCAL_ADMIN_EMAIL` and `COGNIS_LOCAL_ADMIN_PASSWORD` blank.
Do not run the `seed` service for this path. That separate development/demo
shortcut requires a populated provider API key and pre-creates the account,
provider, agent, executor record, and conversation.

Compose configures companion authentication, inference-token volumes, and
storage. Use compatible managed-inference releases. Companions obtain inference
from Cognis and do not need upstream provider keys. For manual Python or
separate-host deployments, follow [Inference API](inference-api.md) and
[Deployment](deployment.md), including JWT/public-key configuration.

## 2. Open setup

The dashboard offers setup while incomplete. You can also use **Settings →
System → Setup guide**. **Documentation** in the menu always opens docs home.

Finishing or permanently closing setup is stored per user on the server,
across devices. It never returns automatically after that, even if configuration
later degrades. Explicitly revisiting it from Settings does not reset dismissal.

## 3. Configure LLM provider

Setup has three screens: **Configure LLM provider → Setup executor → Create agent**.
Only the current screen is shown. Advanced options stay collapsed.

Choose **OpenRouter**, paste an API key and click **Connect and continue**. Cognis discovers
the models, runs a small provider test, and saves the recommended routing
automatically. The test and subsequent usage can incur provider charges.
The key is global so companions can work independently of an agent.

**Image generation is optional but recommended:** leave it enabled to generate
an avatar in the last screen. You can skip it and enable it later.

You do not need to choose models or routes. To inspect or customize them, open
**Advanced options**. The default selections are:

| Role | Recommended upstream model |
|---|---|
| Agent default and companion Smart | `openai/gpt-oss-120b` |
| Classifier and companion Lite | `openai/gpt-oss-20b` |
| Mnemory extraction and retrieval | `openai/gpt-oss-120b` |
| Intaris judge/analysis and Mnemory consolidation/maintenance | Inherit Smart |
| Intaris evaluation | `typesafe/jev-1.13`, or `openai/gpt-oss-20b` |
| Companion embeddings | `openai/text-embedding-3-small` |
| Image generation | A discovered image-capable Gemini or other model |

Automatic defaults use discovered IDs and capabilities, preserve explicit
routes, and do not invent unavailable choices. Existing embedding routes are
never changed automatically. The provider test checks chat access, not every
embedding, evaluation, or image model.

**Jev is a typed-decision model, not a chat model.** Select it for `classifier`
or `intaris_evaluation`, not generation routes. For Intaris evaluation, Intaris must support managed System One inference and use
`EVALUATOR_BACKEND=auto` (`INTARIS_EVALUATOR_BACKEND=auto` in the Compose env),
the local stack's default. For older releases use GPT-OSS-20B until upgrading.

**Embeddings are separate from chat.** Dimensions must match both stores.
The local defaults are 1536, matching the recommended embedding model.
Changing a populated store's model/dimensions requires the migration described
in [Inference API](inference-api.md).

### Custom providers and Codex

Choose **Custom (advanced)** for manual global provider and system-model setup.
The optional **Agent providers** section always remains visible, with shortcuts
for Codex (ChatGPT sign-in), OpenAI, and Anthropic (API keys). Connected accounts
have a checkmark. These accounts are private to you and do not replace the global
models required by Cognis. You can skip these shortcuts and use OpenRouter for
the agent too.

Settings → Providers supports Groq, Codex/ChatGPT subscription authentication,
Anthropic, Ollama, and others. Groq IDs are `openai/gpt-oss-20b` and
`openai/gpt-oss-120b`; a Groq chat provider does not supply embeddings or images.
Agent inference can use a private provider, including Codex. Companion routes
must use global providers. See [Configuring Providers](configuring-providers.md).

## 4. Connect an executor before creating the agent

Choose macOS, Docker, or Kubernetes, then click **Get setup command**. Cognis
creates a private executor and connection code for you. Copy the command into
your terminal and paste the code when asked. Tokens stay separate from the
script so they are not embedded in shell history.

- **macOS:** native Homebrew path: `brew tap fpytloun/tap`,
  `brew install cognis-executor`, interactive `cognis-executor configure`,
  `doctor --test-connection`, and `brew services start cognis-executor`.
  Install Homebrew first if needed; run without sudo. Configuration persists.
  Grant macOS filesystem/browser permissions only as required.
- **Docker:** public `ghcr.io/fpytloun/cognis-executor` image, restart policy,
  and persistent home/workspace. Local HTTP uses host networking with localhost:
  Linux, or Docker Desktop with host networking enabled. Otherwise use HTTPS.
- **Kubernetes:** generated namespace, connection Secret, workspace PVC, and
  single-replica Deployment. Requires kubectl access, a default StorageClass,
  trusted HTTPS, and controller connectivity from pods. No Kubernetes API
  token is mounted.

Run the commands on the target platform and paste the connection token at the
prompt. Regenerating a token invalidates the previous one. Never publish tokens
or disable TLS verification. Pin compatible image versions for production.
Docker/Kubernetes tools access their container and mounted workspace, not your
whole laptop. No Docker socket or cluster-management access is granted.

The screen checks the connection automatically. When your computer is connected,
click **Continue**. Computer name, existing executors, controller URL, token
replacement, and troubleshooting are under **Advanced options**.
See [macOS Executor](macos-executor.md) and [Executors](executors.md) for
permissions, browser installation, persistence, and troubleshooting.

## 5. Create agent

Name your assistant, review the visible **Provider** and **Model** pickers,
optionally choose **Generate avatar**, then click **Start conversation**.
Defaults are selected for you. Cognis uses the XO template and your connected
executor. Your agent can control this instance, create agents, set up tools
and integrations, and manage tasks and workflows. No profile or tool configuration
is required. The page explains the access granted before you create the agent.
Safety checks and approvals stay enabled.

**Advanced options** contains the template, executor, management access,
instructions, and profiles. Templates are also available later in Agents.

**XO** provides coordination, memory, Intaris guardrails, approvals, workflows,
and explicit owner-only management. **Coding agent** provides implementation and
review profiles without agent-management tools.

Templates are independently copied. They never copy deployment-specific model,
executor, or credential references. Missing optional system skills are not
invented. Templates remain available later under Agents → New agent.

## After setup

Open the XO's owner chat and ask it to inspect its tools, run a harmless tool,
remember/recall a preference, and create a coding specialist. Review the
specialist's model, executor, prompt, permissions, and approval requests.
External or delegated conversations do not gain owner-root management authority.

Readiness checks prove configuration only: they do not run inference or tools.
Actual tests are needed for memory, embeddings, evaluation, and images.
Agent creation finishes setup and opens the assistant's owner chat. If the
handoff fails, retry **Start chatting** without creating a duplicate agent.

## Troubleshooting

- Companions unreachable: check URLs, JWT/public key, releases, and service logs.
- Missing models: refresh discovery and verify exact IDs, account access, and credits.
- Jev mismatch: use compatible Intaris with `auto`, or choose GPT-OSS-20B.
- Embedding mismatch: match dimensions; migrate existing collections before changing models.
- Executor unavailable: check logs, token, TLS, runtime state, and applied config.
- Expired account setup URL: restart Cognis or use local `cognis-controller admin create-user`.
