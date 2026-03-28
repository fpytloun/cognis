# Stage 10: Launchable First Run

**Status**: DONE
**Repo**: `cognis`
**Depends on**: Stage 9 (integration testing complete)
**Estimated effort**: 3-4 days

## Objective

Make `uvx cognis` land users in a working browser flow on `:8080` — setup,
login, and into the app — without CLI workarounds, separate Node processes,
or guesswork. After this stage, a fresh install produces a usable web
application from a single command.

## Context

The current MVP has a broken first-run path:

- The setup URL (`/setup?token=...`) renders a bare HTML stub telling the
  user to "Use POST /api/setup" — there is no form.
- The SvelteKit UI runs as a separate Node process on `:5173` and must be
  started manually with `npm run dev`.
- Startup messaging does not indicate whether companion services (Mnemory,
  Intaris) are reachable.
- Users who miss the 15-minute setup token window have no UI recovery path.

These issues mean the very first interaction after install fails for any
user who is not reading source code.

## Deliverables

### 1. Bundled UI Serving

Build the SvelteKit app into static assets and include them in the Python
wheel so FastAPI can serve the full SPA from `:8080`.

- **Hatch build hook**: add a custom build hook (or `hatch-build` script)
  that runs `npm ci && npm run build` inside `ui/` during `hatch build`.
  The output (`ui/build/client/`) is included as package data.
- **SPA fallback mount in `app.py`**: on startup, if the built UI directory
  exists, mount it via Starlette `StaticFiles`. Serve `/api/*`,
  `/.well-known/*`, and `/api/ws` normally; all other paths fall through
  to `index.html` for client-side routing.
- **`COGNIS_SERVE_UI` env var**: defaults to `true`. When `false`, the
  static mount is skipped (for production split deployments where the UI
  runs as a separate service).
- **Dev mode unchanged**: developers still run `npm run dev` on `:5173`
  with the Vite proxy to `:8080`. The embedded mount is ignored when the
  dev server is running.
- **Adapter switch**: change `ui/svelte.config.js` from `adapter-node` to
  `adapter-static` so the build output is pure static files (no Node
  runtime needed at serve time). SSR is already disabled (`ssr = false`).

### 2. Setup UI Page

Replace the dead-end HTML stub with a real setup form in the SvelteKit app.

- **New route `ui/src/routes/setup/+page.svelte`**: reads `?token=` from
  the query string. Shows a form with email, name, password, confirm
  password.
- **Client-side validation**: required fields, email format, password
  minimum length (8 chars), password confirmation match.
- **Submit**: POSTs to `POST /api/setup` with the token. On success,
  auto-logs in (calls `/api/auth/login`) and redirects to `/chat`.
- **Error states**: expired/invalid token shows a clear message with CLI
  fallback instructions (`cognis admin create-user`). Already-setup state
  (users exist) shows "Setup already completed" with a login link.
- **Backend change**: replace the bare HTML in `cognis/api/routes/auth.py`
  `GET /setup` with a redirect to the SPA `/setup` route (or remove it
  entirely if the SPA handles the path).

### 3. First-Run Readiness Page

After first login, show a readiness checklist so the user knows what to
configure next.

- **Readiness component**: displayed on the chat page (or as a modal/banner)
  when the system detects first-run conditions (no LLM provider configured,
  no agents created).
- **Checklist items**:
  - Mnemory reachable (green/red with URL, from `/api/health`)
  - Intaris reachable (green/red with URL, from `/api/health`)
  - LLM provider configured (green/red, link to Settings > Providers)
  - At least one agent created (green/red, link to Agents > New)
- **Dismissible**: user can close it. Does not block navigation.
- **Re-accessible**: available from Settings > System tab.

### 4. Startup Messaging Improvements

Make the console output on `cognis serve` honest and actionable.

- **Setup URL**: print the URL pointing to the SPA `/setup` route, not
  the bare HTML endpoint.
- **Companion service check**: on startup, probe Mnemory and Intaris health
  endpoints. Print status: "Mnemory: reachable at http://localhost:8050"
  or "Mnemory: NOT reachable at http://localhost:8050 — memory features
  will be unavailable".
- **UI status**: if `COGNIS_SERVE_UI=true` and UI assets are present,
  print "Web UI: http://localhost:8080". If assets are missing, print
  a warning: "Web UI assets not found — run `cognis ui build` or set
  COGNIS_SERVE_UI=false".
- **Bundled mode**: when UI is served, print a single URL for the user
  to open, not separate API and UI URLs.

### 5. Dockerfile

Single image that can serve both backend and frontend, or either alone.

- **Multi-stage build**:
  - Stage 1 (Node): `npm ci && npm run build` in `ui/`
  - Stage 2 (Python): copy built UI assets, install Python package
- **Entrypoint**: `cognis serve` (default, serves both). Environment
  variable `COGNIS_SERVE_UI=false` for API-only pods.
- **Same image for all roles**: production k8s can run API-only pods and
  UI-only pods (via nginx sidecar or static serving) from the same image.
- **Health check**: `HEALTHCHECK CMD curl -f http://localhost:8080/api/health`

## Acceptance Criteria

- [ ] `pip install cognis` (or `uvx cognis`) includes built UI assets
- [ ] `http://localhost:8080` serves the SvelteKit app without a separate
      Node process
- [ ] `/setup?token=...` shows a real form with validation
- [ ] Completing setup auto-logs in and redirects to `/chat`
- [ ] Expired/invalid token shows actionable error with CLI fallback
- [ ] First-run readiness checklist appears after first login
- [ ] Readiness checklist is dismissible and re-accessible
- [ ] `COGNIS_SERVE_UI=false` disables static serving
- [ ] Dev workflow (`npm run dev` + `cognis serve`) still works unchanged
- [ ] Docker build produces a single image
- [ ] Docker image supports both combined and API-only modes
- [ ] Startup logs show companion service reachability
- [ ] Startup logs print the correct URL for the user to open

## Key References

- `cognis/api/app.py` — FastAPI factory, static mount location
- `cognis/api/routes/auth.py:25` — current dead-end setup HTML
- `cognis/bootstrap.py` — first-start bootstrap logic
- `cognis/main.py` — Typer CLI entry point, startup messaging
- `ui/svelte.config.js` — adapter configuration
- `ui/vite.config.ts` — dev proxy configuration
- `pyproject.toml` — hatch build configuration
- `docs/specs/11-deployment.md` — deployment spec
