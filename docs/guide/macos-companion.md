# macOS Companion (developer preview)

The companion is a small AppKit host for the **bundled Cognis web UI**, not a
second chat client. Requires macOS 15+, Xcode/Swift 6 and Node/npm for builds.
This preview is local-only; development bundles use whole-bundle ad-hoc signing.
Physical GUI and communication-avatar acceptance are separate gates. M2 added build/update foundations, optional
existing-executor controls, resident native notifications and opt-in login/
background restore. See [`apps/apple/PLATFORM.md`](../../apps/apple/PLATFORM.md)
for configuration, defaults and separate OS/signing/APNs gates.

## Build and run

From the same source checkout on a Mac:

```sh
bash apps/apple/build.sh
open apps/apple/.build/Cognis.app
swift test --package-path apps/apple --filter CompanionContractTests
```

The script uses the locked npm dependencies, builds the current UI with
`COGNIS_APPLE_BUNDLE=1`, compiles Swift, and copies the export into the generated
`.app`. Build output stays under ignored paths. About / Diagnostics shows native
version/build, web version, source commit, bridge version
and connection state. Different web/server release numbers are not by themselves
an incompatibility.

For a Linux-built web export copied to a task-owned Mac checkout, run
`python3 apps/apple/package_info.py stamp-web` immediately after export.
Transfer the exact source plus `ui/build-apple` and `ui/build-apple-manifest.json`,
then run `COGNIS_WEB_PREBUILT=1 bash apps/apple/build.sh`. Mismatched source/build
inputs or changed assets are rejected. Never restamp an untrusted/stale export.

## Connect and use

Choose an HTTPS **origin**, without a path, credentials, query or fragment.
Loopback HTTP (`localhost`, `127.0.0.1`, `::1`) is allowed for development only.
There is no production server default. Login and onboarding use the normal UI.
“Change Server…” closes the previous native projection and replaces its web
runtime with an isolated persistent WebKit data store. Returning to a server
restores that server's cookies, localStorage and IndexedDB.

Use the existing conversation Pin button. Resolved conversations appear as
agent-initial icons on the desktop. Multiple pins for one agent share an icon
and a conversation menu. Drag the icon rim to move the avatar rail vertically or
onto another physical display. The rail docks to the configured left or right edge
of the display under the pointer. Settings → Rail / Display also lets you choose
the display. Placement is remembered per server on this Mac, including across
app restarts. If the chosen display is disconnected, the rail temporarily uses
the primary display and returns when the chosen display reconnects. Membership is owned by
the existing web pin store; Safari/PWA pins are a separate device-local collection.
Unresolved agent shortcuts do not silently create a conversation.

The menu-bar item starts with **New conversation**, every available non-system,
enabled agent, then **Settings…**. Agent selection opens or focuses the compact
tray chat through the canonical agent-direct resolver, even with no pins or open
windows. It does not pin the conversation. New conversation opens the existing
picker without sending a message. App management, server selection, diagnostics
and Quit remain available below Settings.

Settings is native and client-local (also **Cmd+,**). It groups desktop rail,
display placement, notification previews/macOS controls and startup options.
Half-hide defaults on only when unset; an explicit stored off value is preserved.
The main authenticated web workspace remains the half-hide persistence owner.
There is no server-side Desktop preference page or window-management preference.

### Panel keyboard shortcut and app links

**⌃⌥⌘C** toggles the compact panel without changing the main window. Settings →
Keyboard shortcut records another combination (include Command, Control or
Option); Escape cancels recording and Delete disables the shortcut. Registration
status shows conflicts with another app. The shortcut works only while Cognis
is running; enable its login item under Startup for use after login. It requires
no Accessibility permission. Remove any older external hotkey helper separately
after installing the new app; do not leave both helpers claiming the same keys.

The target is the last agent whose pinned **or direct** panel was shown, remembered
on this Mac separately for each server. If that agent is no longer available,
use the first resolved pin, then the first available agent. There is no default-agent
setting. Pinned panels restore their last selected tab (`lastSelectedKey`), or Home
when there is none. Unpinned agents open their Home/direct conversation; a pinned
agent whose Home is still unresolved keeps the pinned presentation.
Every panel open focuses its editable composer. A second press hides the panel;
open sheets/menus are not dismissed. Closing an externally opened panel returns
focus to the previous app when no other Cognis window is visible.

Shortcuts **Open URLs** and Raycast quicklinks can use:

```text
cognis:///native-pinned
cognis:///native-pinned?agent=agent_id
cognis:///native-pinned?agent=agent_id&conversation=conversation_id
cognis:///native-pinned?agent=agent_id&direct=1
cognis:///chat/conversation_id
cognis:///chat/conversation_id?window=1
cognis:///chat?scope=agent_direct&agent_id=agent_id
cognis:///tasks/task_id
cognis:///projects/project_id
cognis:///library/artifact/artifact_id
cognis:///library/deliverable/deliverable_id?window=1
```

Replace the identifiers with real Cognis identifiers. An explicit panel
conversation must already be a retained tab of that agent; otherwise Cognis
shows an alert instead of opening a different conversation. Use `/chat/<id>` for
an arbitrary accessible conversation. The agentless panel URL
uses exactly the hotkey's target and toggle behavior. Other accepted main-window
destinations are `/`, `/chat/new`, `/tasks` and `/notifications`.
`?window=1` is supported only for conversations, projects and deliverables.
An empty host means the active server; `cognis://host[:port]/…` must match the
active server's host/port. A mismatched server raises an alert and **never**
switches or adds a server. Signed-out users must sign in first.

App links only navigate/show windows: they cannot send text, approve, delete,
or run tools. Opening Home may resolve/create the caller's agent-direct
conversation, just like clicking the rail; observing a visible focused chat
can mark it read. Server authorization still applies. Unknown routes, extra
parameters, fragments, credentials and encoded path segments are refused.
The standard app registers `cognis`; Preview registers `cognis-preview`;
Acceptance registers neither. Do not launch development standard builds when
testing the installed app's URL binding, as macOS may choose either copy.

References in messages remain canonical relative or HTTPS web routes. On the web
they behave unchanged. In the native app, supported same-server link clicks open
inside Cognis (new-window links reuse native standalone windows); other links
keep opening the browser. Custom-scheme links inside web content cannot invoke
the OS URL handler. Universal Links and iOS presentation are a later phase;
the Foundation-only `AppLink` destination contract is reusable there.

The main authenticated web document remains resident. Native surfaces receive
typed, account-fenced projections and use the same chat implementation. Existing
draft/offline persistence is unchanged. Escape dismisses compact chat; explicit
Quit terminates the web runtime and native panels.

The chat and project **Open in new window** actions and the deliverable
three-dot menu open isolated native windows, not browser popups. Opening the
same kind and identifier again focuses its existing window. All three kinds
share a limit of ten standalone windows; reload reconstructs the same destination,
and signing out or changing servers closes them together. Browser builds keep
their existing popup behavior. This support requires an updated native app bundle.

## Downloads and exports

Inline previews use the same renderers as the web app: PDF (page navigation and
zoom), images, HTML, Markdown, Mermaid, CSV/TSV, text/code, audio, and video.
PDFs initially fit the whole page without enlarging beyond 100%. Use the zoom
percentage field or −/+ for 10–400% zoom, **100%** for actual CSS size, or the
explicit **Fit page** and **Fit width** actions. Manual zoom stays fixed when
resizing the window or changing pages; fit modes adapt to the available space.
PDF workers, fonts, CMaps, and image decoders ship with the app; previewing does
not load executable code from the connected server. Audio/video support still
depends on WebKit's installed codecs. HTML keeps its sandbox and self-contained
resource requirements. Unsupported file formats remain downloadable.

Native PDF previews use the shipped JavaScript JPEG2000/JBIG2 decoders because
the PDF.js WASM builds require relaxed SIMD unavailable in WebKit. Their
runtime imports are compiled into the bundled worker; a build-time guard requires
revalidation when PDF.js changes this contract. Web previews keep their normal
WASM and worker-fetch paths.

Download controls open a native **Save** sheet in the active main, compact,
or standalone conversation, project, or deliverable window. This includes attachment originals, Library and
knowledge files, document PDF/HTML/Markdown, audio parts, skill assets/packages,
workflow YAML, recovery codes, and chat diagnostics.

Choose a filename and location, or cancel without creating a file. A download
is staged privately beside the selected location and replaces the chosen file
only after it completes successfully. Errors leave the existing file intact
and show a retry message. Account changes, server changes, window teardown,
and document reloads cancel pending downloads and remove their staging files.
Only one save can be active in a window. Ordinary external links still open
in the default browser; sandboxed HTML previews and the desktop rail cannot
start downloads or open Save sheets.

## Notifications

Notifications show bounded plain text, agent name and conversation/task title.
Exact reply previews require the controller's `completed_reply` sidebar metadata;
older controllers keep metadata-only alerts, never a fetched later "latest" reply.
Code, links and credential-bearing lines are omitted. Preview display is controlled
locally and remains subject to macOS notification/lock-screen settings.

Ad-hoc builds (including development Preview) use standard notifications with
agent name, conversation/task title, text and existing review/approval actions.
Communication-style avatars are unavailable; diagnostics report
`unavailable-standard-notifications`. No communication intent update or donation
is attempted. The app icon is the supported fallback.

Every ad-hoc signing target, including nested apps and XPC services, receives
explicitly empty entitlements. Final signatures are inspected to reject the
restricted communication entitlement. A strict resource seal is necessary but
does not prove kernel launch acceptance: actual macOS launch is a separate gate.
Future communication capability requires explicit supported Apple provisioning;
it must not be enabled merely by selecting a signing mode.

Simple tool approvals can offer **Approve / Deny** (unlock required); an ordinary
click is **Review**. The resident authenticated client revalidates account, scope,
action, expiry, revision and server permission, then submits through the normal
attention API with a unique submission ID. No approval is queued offline or
replayed after sign-in. Credentials, MFA and questions requiring input remain
Review-only. Success is shown only after server acknowledgment.

## Download regression checks

The browser fixture bundles production download controls and helpers, not a
product test route:

```sh
cd ui
npx playwright test --config playwright.downloads.config.ts
```

On an unlocked Mac, build the same fixture and leave its loopback-only server
running in a separate terminal:

```sh
cd ui
node scripts/build-download-fixture.mjs ../.local/download-fixture
node scripts/serve-download-fixture.mjs ../.local/download-fixture 43128
```

Then, from the repository root:

```sh
COGNIS_DOWNLOAD_FIXTURE="$PWD/.local/download-fixture" \
COGNIS_DOWNLOAD_ORIGIN=http://127.0.0.1:43128 \
python3 apps/apple/tests/run_gui_tests.py --filter 'DownloadTests|DiagnosticSaveTests'
swift test --package-path apps/apple --filter CompanionContractTests
```

The GUI runner loads the unmodified XCTest bundle in a task-local foreground
AppKit application under `.local/native-gui-tests`. This gives the test windows
a real application lifecycle; plain command-line XCTest can remain inactive
even on an unlocked desktop. The runner requires actual key-window ownership
and fails on missing tests, skipped tests, or test failures. It does not alter
installed applications, preferences, or permissions.

These checks use isolated, randomly named WebKit profiles and synthetic files.
They exercise a real Save panel, blob and HTTP bytes, authenticated downloads,
redirects, failure/retry, cancellation, child-frame refusal, and synchronous
staging cleanup. Do not treat browser-only results as native Save-panel evidence.

For the native preview matrix (requires Node/npm and `ffmpeg` to generate
synthetic media):

```sh
cd ui
node scripts/build-artifact-preview-fixture.mjs ../.local/artifact-preview-fixture
node scripts/build-html-preview-fixture.mjs ../.local/html-preview-fixture
cd ..
COGNIS_ARTIFACT_PREVIEW_FIXTURE="$PWD/.local/artifact-preview-fixture" \
COGNIS_HTML_COMPONENT_FIXTURE="$PWD/.local/html-preview-fixture" \
python3 apps/apple/tests/run_gui_tests.py \
  --filter 'ArtifactPreviewTests|HTMLPreviewTests.testCompiledComponents'
```

This checks actual PDF and JPEG2000-image PDF pixels, page navigation, bundled
CMaps and font loading, worker cleanup, invalid-PDF error containment,
image decoding, rich/text rendering, audio/video readiness, and sandboxed HTML
across chat, Library, inline, and project surfaces. It does not install an app or
access the signed-in profile.

For the corresponding web PDF path, including separately served decoder assets:

```sh
cd ui
node --test scripts/native-pdf-worker.test.mjs
npx playwright test --config playwright.previews.config.ts
```

## Isolated local acceptance stack

Use a fresh checkout/worktree, not an existing production, alpha or M1 stack.
M2 owns project `cognis-macos-platform-m2` and loopback port **18763**:

```sh
docker compose -p cognis-macos-platform-m2 \
  -f compose.local.yml -f compose.e2e.yml -f apps/apple/compose.platform-acceptance.yml \
  up -d --build cognis mock-llm qdrant
uv run --all-packages --extra dev python apps/apple/tests/platform_smoke.py
cd ui && npx vite-node scripts/check-native-replay.ts
```

The overlay uses an explicitly **test-only** seeded account:
`mac-companion@cognis-e2e.localdev.me` / `mac-companion-local-acceptance`.
Never use this password or overlay for a real server. No executor is started.
The smoke checks real discovery/cookie auth, one owner-wide WebSocket row,
inbox/logout and revoked-cookie rejection. Its separate terminal-state replay
is **injected**, not evidence of an LLM completion or OS notification delivery.

On a separate Mac, establish a loopback-only SSH tunnel to that machine using
your already configured SSH identity:

```sh
ssh -N -o ExitOnForwardFailure=yes \
  -L 127.0.0.1:18763:127.0.0.1:18763 YOUR_DEVELOPMENT_HOST
```

Acceptance packages are pinned to `http://127.0.0.1:18763`, rejecting M1's port.
For actual packaged resource/cold-storage WebKit checks:

```sh
COGNIS_TEST_WEB_ROOT="$PWD/ui/build-apple" \
swift test --package-path apps/apple --filter 'testPackaged'
```

This opt-in test clears **only that task loopback profile's** WebKit data at its
start. Do not run it against a profile whose manual draft/pin state you need.
It does not inject auth cookies or local auth state. The retained opt-in
full-send M1 test additionally needs its deterministic agent/executor fixtures;
it is not exercised by the M2 no-executor smoke. Standard contract and injected
platform tests do not require Docker or network.

Cleanup only this project and its task-owned tunnel:

```sh
docker compose -p cognis-macos-platform-m2 \
  -f compose.local.yml -f compose.e2e.yml -f apps/apple/compose.platform-acceptance.yml down
```

Volumes intentionally remain for restart acceptance. Remove them only after
explicitly deciding to discard this task's test data. Never run broad Docker
cleanup or stop other projects.

## Acceptance checklist (record actual results separately)

- Real login → create conversation with deterministic E2E agent → send → streamed
  response → pin → desktop icon → close main → icon → compact send once → answer.
- Open main and verify the same conversation; dismiss/reopen compact with an
  unsent draft; quit/relaunch and verify server, pins, cookies and draft persist.
- Disconnect only the task tunnel; verify cached chat and draft, queue a send,
  reconnect and verify a single admitted message/answer and catch-up.
- Hide, minimize and fully occlude main/compact: no read receipt while hidden or
  unfocused. New unread/attention changes only the appropriate icon/menu summary.
- Repeated icon taps reuse one surface. Multiple conversations for one agent
  remain distinct menu items. Sign out/server change clears old panels.
- Move icons and compact chat across displays; disconnect a monitor and verify
  clamping. Change Spaces, enter another app's fullscreen Space, and check
  `.canJoinAllSpaces` / `.fullScreenAuxiliary` behavior permitted by macOS.
- Type and select a file in compact chat; Escape closes without submitting.
  Verify external HTTPS links open in the default browser, not inside the host.
- Capture main/menu-bar and normal desktop/another-app + compact/icon screenshots.
  Never substitute a browser preview for OS integration evidence.

`apps/apple/scripts/ax.swift` is a developer-only AX inspection helper for this
task app. It uses only already granted Accessibility access and never requests
permission. An unlocked console is needed for meaningful interactive acceptance.
The app itself does not require Accessibility or Screen Recording. If desktop
capture is not already permitted, have the user capture screenshots manually;
do not grant permissions as a workaround. Mark untested OS cases honestly.

## Deferred

iOS/iPadOS, true APNs while quit/asleep, sharing extensions, widgets/Live
Activities, App Intents, global configurable hotkeys, cross-device pins, voice
platform changes and public releases remain deferred. M2 does not claim real
update installation, signing/notarization, physical login/reboot, locked delivery
or M1 GUI acceptance. No unattended permission/login registration is performed.
