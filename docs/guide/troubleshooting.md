# Troubleshooting

## Setup problems

- **Setup page says the token is invalid or expired**
  - Restart Cognis to print a new setup URL.
  - Or create the first user locally with `cognis admin create-user ...`.

## UI problems

- **The browser opens `:8080` but no UI is shown**
  - Confirm bundled UI assets are present.
  - If you intentionally split deployments, set `COGNIS_SERVE_UI=false` and serve the UI separately.
- **The UI loads but setup still looks incomplete**
  - Open the onboarding guide or system diagnostics and check which readiness step is still pending.
  - Confirm that a provider and at least one agent already exist.

## Provider problems

- **Provider test fails with auth error**
  - Check the environment variable for the provider API key.
- **Provider test fails with connection error**
  - Check proxy/base URL settings or Ollama availability.
- **Provider test fails with model not found**
  - Verify the configured default model.

## Executor problems

- **Tools are missing for an agent**
  - Confirm the selected executor has those tool groups enabled.
  - Confirm the agent did not disable the tool category or tool itself.
- **A provider or channel depends on a remote executor but does not work**
  - Confirm the executor is connected and healthy.
  - Confirm labels or explicit executor binding resolve to the expected executor.

## Companion service problems

- **Mnemory unreachable**
  - Verify `COGNIS_MNEMORY_URL`.
- **Intaris unreachable**
  - Verify `COGNIS_INTARIS_URL`.
- **JWT validation fails in companion services**
  - Confirm they are using Cognis's public key or JWKS endpoint.

## API key problems

- **API key stopped working**
  - Check whether it expired.
  - Revoke and create a new one from **Settings → Account** if needed.

## Docs and onboarding problems

- **The onboarding guide links out to the wrong place**
  - Use the in-app `Docs` section for bundled user guidance.
- **A docs page does not match the current UI**
  - Check whether the running instance is older than the repository docs and bundled UI assets.
