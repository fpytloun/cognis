# Mac Companion M1 host contract

## Loading and transport

`apps/apple` uses AppKit and WebKit, with no third-party runtime framework.
Public `WKWebView.loadSimulatedRequest` loads the bundled SPA HTML with the
selected server's HTTP(S) origin. SvelteKit's native export points executable
assets at `cognisbundle://app`; the host also rewrites the bootstrap's special
server-relative `/_app/env.js` reference to its bundled equivalent.

A read-only `WKURLSchemeHandler` serves only files inside the bundle after
normalized/symlink-resolved path confinement. Asset CORS permits opaque module
origins because nested custom-scheme ES modules have an opaque origin; **these
responses contain only public bundle assets**. Backend CORS, cookie SameSite
and TLS validation are unchanged.

The document remains first-party to the selected server for HttpOnly cookie
login/refresh, HTTP, WebSocket and relative media URLs. There is no local HTTP
server, network proxy, token extraction or native auth implementation. Each
normalized server origin maps deterministically to a distinct persistent
`WKWebsiteDataStore` identifier, including separate ports. Web offline storage
continues to partition by its existing verified-user owner inside that store.

There is no service-worker entry point in the native build. Native runtime
configuration is injected before boot and takes precedence over public build
environment API settings. Normal browser builds keep their existing behavior.

## Trust boundary

Only the single bundled top-level document may post bridge messages. A native
instance verifies main frame, owning WKWebView, selected origin, protocol
version, document generation, shape and size. Navigation delegates cancel all
replacement/child application frames; reload rebuilds the trusted runtime.
Explicit user-activated HTTPS/mailto (or development-loopback HTTP) links open
through `NSWorkspace`. Arbitrary custom schemes and credential-bearing links
are rejected.

The CSP excludes server-hosted executable scripts, remote application frames,
objects and forms. It permits only bundled executable assets and the inline
bootstrap emitted by SvelteKit. File selection requires the trusted top-level
frame in a key app window and uses the normal user-controlled open panel.

Bridge v1 carries only:

- web → native: bounded pin/agent identity and labels, summary attention class,
  auth lifecycle epoch, connection state, current internal route, dismissal;
- native → web: structured internal navigation and visibility/focus state.

No token, raw message, tool arguments, hidden reasoning, arbitrary JS, command,
filesystem or network capability is exposed. Native calls a fixed JavaScript
expression with structured arguments, not JS supplied by a bridge caller.
Incompatible bridge versions fail clearly; server release-number differences
are not an automatic rejection.

Standalone chat windows retain the existing `open-window` message with only
`conversationId`. Project and deliverable windows use `open-resource-window`
with exactly `kind` (`project` or `deliverable`) and `id`. The native contract
constructs allowlisted routes from validated identifiers; callers cannot supply
a URL or path. Typed destinations namespace window reuse and own reload/reopen
identity. All kinds share the existing ten-window registry, account/server
retirement, workspace fencing and download teardown. Resource documents use the
`resource` native surface and the same trusted-frame/key-window save admission.

## Ownership and lifecycle

The existing web pin store is the sole membership owner. Native pins are
ephemeral projections grouped by agent, with resolved conversation IDs preserved.
Native owns window geometry and the desktop display toggle only. It does not
persist pin membership or chat content. An immediate empty epoch fence clears
the projection on account/logout transitions, independently of summary coalescing.

M1 intentionally moves **one WKWebView** between main and compact windows.
This provides one composer/draft owner, existing idempotent web outbox and
realtime client rather than adding a cross-window protocol. Main cannot remain
an invisible competing chat while compact is open. Closing either surface
fences observation and navigates to `/companion`, whose page has no chat
timeline. The authenticated shell keeps the existing pin-summary runtime.
No per-pin web views/timelines are constructed.

Observation requires native visible + key/focused + active state in addition
to existing web conditions. Both read trackers and the API read boundary gate
receipts; the chat page also gates optimistic local reads. Profile replacement
invalidates bridge generation/callbacks, navigates away from the old document,
closes panels and constructs a new isolated web runtime. Explicit Quit removes
handlers/observers, closes native windows and terminates the app process.

## Extension seams

Keep the bridge small and versioned. Future targets can supply capabilities
without replacing web chat/auth/domain reducers. Distribution can version and
sign this bundle without changing its storage or chat ownership. Simultaneous
independent chat windows would require an explicitly designed shared draft/
outbox ownership contract and are not implicitly enabled by M1.
