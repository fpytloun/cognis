# Troubleshooting

## Setup problems

- **Setup page says the token is invalid or expired**
  - Restart Cognis to print a new setup URL.
  - Or create the first user locally with `cognis admin create-user ...`.

## UI problems

- **The browser opens `:8080` but no UI is shown**
  - Confirm bundled UI assets are present.
  - If you intentionally split deployments, set `COGNIS_SERVE_UI=false` and serve the UI separately.

## Provider problems

- **Provider test fails with auth error**
  - Check the environment variable for the provider API key.
- **Provider test fails with connection error**
  - Check proxy/base URL settings or Ollama availability.
- **Provider test fails with model not found**
  - Verify the configured default model.

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
