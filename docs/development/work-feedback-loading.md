# Work Feedback loading boundaries

The startup budget remains 420 KiB Brotli. This change is a loading refactor,
not a change to feedback permissions, annotation algorithms or send semantics.

## Attention content

`AttentionActionDialog` and `WorkspaceLayer` remain eager hosts. Only
`FocusedAttentionAction` is deferred, through the shared definition cache and
private deferred-view boundary. Dialog history/navigation, blocking overlay,
focus, Escape and close handling run before its content download completes.
Workspace window keys and layer lifecycle are unchanged. Action replacement
remounts only the content; mode removal and account invalidation cancel stale
mounts. A download error stays in the host with retry and close controls and
never invokes action settlement or unavailability callbacks.

## Eager owners

- `feedback/transport.ts` owns the installed HTTP adapter. The app layout must
  not import a component or the full feedback API to install that adapter.
- `FeedbackComposerTray` subscribes to staging, validates inclusion, publishes
  counts and calls `onReadyChange` before any feedback chrome is requested.
  Main, compact and task composers use this same eager boundary.
- `FeedbackDeliveryModel` observes the canonical chat outbox and retains the
  original transaction. Its receipt check and retry are explicit user actions.
  Recovery does not depend on downloading its presentation component.
- The existing conversation composer, admission and outbox owners still send
  one ordinary turn with the original attachments and mode.

## Deferred owners

`FeedbackChromeMount` is the shared private presentation boundary. Its caller
supplies a typed definition rather than asking an auth factory to choose one.
Panel, review and SPA delivery have separate entry definitions: a hosted owner
viewer must not pull SPA outbox recovery into its graph, even dynamically.
Public capability clients and offline viewers never fall back to these owners.

`DeferredPrivateView` caches component definitions, never props, user data,
controllers, leases or receipts. Cold completion is fenced by mount lifetime,
origin context, account invalidation, profile/native generation and any
caller-supplied lease assertion. Modal loading has explicit Close/Retry,
Escape, focus restoration and a pending focus trap. Warm prop changes retain
the same component instance and DOM.

Diff registries capture their document lease before loading the controller.
They reject completion after disposal or origin replacement. A registry is
replaced when its owning origin changes; it cannot carry an old chat target
into a newly rendered source.

Baseline diff, image, audio and code-modal loading is changed only at private
callers. Renderer, geometry, selection and bridge implementations are unchanged.
An audio mount never replays a synthetic play gesture after an await.

## Qualification

Keep the source frozen while qualifying. Do not run SvelteKit build/sync
generators against a worktree serving browser regressions: generated server
nodes can be replaced underneath the dev server. Run tests, check, standalone
build and production build first; then start a fresh dedicated browser server.

Run the feedback/model and all-three-composer suites, Work retention tests,
deferred-view focus/cancellation tests, existing renderer tests and the rich
dashboard layout regression. Run `npm run check`, `npm run build:standalone`
and `npm run build` with the unchanged bundle policy.

The production audit must calculate Brotli from the **final written files**:
Vite can rewrite preloads after `generateBundle`. Follow static `imports`
from the canonical budget roots, not dynamic imports. Verify that panel,
review/access/submission/saved-capture/delivery UI and baseline modal/render
definitions are absent, while staging, tray, transport and ordinary outbox
owners remain present.

For browser checks, use an isolated loopback server and explicit API fixtures:

```bash
npm run dev -- --host 127.0.0.1 --port 5184 --strictPort
COGNIS_E2E_URL=http://127.0.0.1:5184 npx playwright test \
  e2e/work-feedback-loading.spec.ts --project=chromium --workers=1
```

The development-only loading fixture never establishes authority itself: it
uses the normal authentication bootstrap, which browser tests intercept.
These browser fixtures qualify loading behavior, not real-endpoint acceptance.
Integration acceptance and source screenshots remain with the integration owner.
