# Lumen showcase

**Unfinished demo fixture.** The recorded Porcelain LIGHT walkthrough omits
Usage session/request/attempt detail and conversation Usage. Reviewer and
anonymous permission gates and an independent clean restore are not qualified.
The recording is for visual review, not final acceptance; see `CHECKPOINT.md`.

The seeded conversations, code changes, command output and 30 days of priced
usage are fictional fixtures, not executed coding work, observed performance or
billing. Tasks and the feedback revision run through the real authenticated
workflow and feedback runtime; a local deterministic mock model chooses the tool
calls. That is real local orchestration, not real model inference or paid usage.
Usage captures end yesterday: live mock turns from today report no token usage
and would show as unpriced.
The fixture
files form one cohesive directory and must all be present before seeding. A partial
or conflicting fixture fails visibly: use a fresh runtime and Compose project,
not database surgery.

## Fresh runtime

From the repository root, these commands create a new private directory and
random project, reserve different loopback ports, build the current checkout
under the shared validation lock, and record running image identities:

```sh
export RUNTIME="$(mktemp -d /tmp/lumen-showcase.XXXXXXXX)/runtime"
python3 tests/e2e/showcase/prepare.py "$RUNTIME"
export PROJECT="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["project"])' "$RUNTIME/runtime.json")"
make validate COMMAND="docker compose --project-name $PROJECT --env-file $RUNTIME/compose.env -f compose.local.yml -f compose.e2e.yml -f tests/e2e/showcase/compose.yml up -d --build"
python3 tests/e2e/showcase/prepare.py --attest "$RUNTIME"
make validate COMMAND="docker compose --project-name $PROJECT --env-file $RUNTIME/compose.env -f compose.local.yml -f compose.e2e.yml -f tests/e2e/showcase/compose.yml run --rm showcase-seed"
```

The seed creates the Lumen workspace: the agent crew Atlas, Forge, Iris and Vela
with the portraits in `avatars/` (the same crew as the product film), three
projects, seeded conversations with code changes, command output and research
history, three schedules, four tasks completed through the general-task
workflow by the local mock model, and 30 days of priced usage attributed to
agents, projects, conversations and task sessions. Seeded history and usage
are demo data; nothing in it was executed or billed.

Ports are selected before Compose starts, so a competing bind can still make
startup fail; never use an existing service as a fallback. Compose volumes and
image names are project-specific. The runtime is outside Git and contains
credentials: do not publish `compose.env`, `private.env`, `runtime.json`, the
token directory, logs containing credentials, or database files. `private.env`
is mode 0600. `manifest.json` contains no passwords and is written only after
the complete content seed succeeds.

## Capture

Install the UI's locked dependencies and Chromium normally, then capture from
the repo root using a fresh private output directory:

```sh
make validate COMMAND="npm --prefix ui ci"
make validate COMMAND="ui/node_modules/.bin/playwright install chromium"
export SHOWCASE_MANIFEST="$RUNTIME/manifest.json"
export SHOWCASE_PRIVATE_ENV="$RUNTIME/private.env"
export SHOWCASE_OUTPUT="$(mktemp -d /tmp/lumen-capture.XXXXXXXX)"
make validate COMMAND="ui/node_modules/.bin/playwright test --config tests/e2e/showcase/playwright.config.ts --project comparison"
```

The comparison contains five views in each of Classic, Porcelain and Paper
LIGHT plus a matched Usage view in each family's DARK mode. The reviewed
demo/docs baseline is **Porcelain LIGHT**, not a
production-global setting. Preserve the comparison when choosing a different
baseline. The feature run requires the real mock-backed feedback path to have
completed. It expands the coding inspector and diff rather than treating the
narrow sidebar preview as a final coding capture.

Published chat and Work views use the seeded `Checkout · saved carts`
conversation; activity probes run only in their own conversations.

Use the same manifest and comparison output:

```sh
export SHOWCASE_THEME=porcelain
export SHOWCASE_SCREENSHOTS_ONLY=1
make validate COMMAND="ui/node_modules/.bin/playwright test --config tests/e2e/showcase/playwright.config.ts --project feature"
```

For task-outcome chat acceptance, seed a fresh runtime and capture:

```sh
make validate COMMAND="docker compose --project-name $PROJECT --env-file $RUNTIME/compose.env -f compose.local.yml -f compose.e2e.yml -f tests/e2e/showcase/compose.yml run --rm -e SHOWCASE_TASK_OUTCOMES=1 showcase-seed"
make validate COMMAND="ui/node_modules/.bin/playwright test --config tests/e2e/showcase/playwright.config.ts --project task-outcomes"
```

This optional fixture adds
illustrative completed/failed task history, partial output and a handling row.
It exercises the real chat/task/document APIs, not actual execution or recovery.

Omit `SHOWCASE_SCREENSHOTS_ONLY` only when recording has been authorized.
The seed validates completion references, ownership, credentials, input digest
and Intaris events before it skips all creator functions. Do not
claim capture success without inspecting its output and running the actual
browser test.

### Documentation screenshot inventory

`screenshot-inventory.csv` maps the 66 original screenshot paths and selected
supplemental views to their required state, viewport, theme and named capture
step. Rows marked `PENDING` or `GAP` block complete screenshot parity.

Desktop documentation views use a 1280x800 CSS viewport at device scale 2.
GitHub scales README images to its roughly 900-1000 px column, so this size
keeps product text readable there; full-window 1440-1800 px captures shrink it
to half size. GIFs are 960 px wide at 15 fps. The
original mobile image files measured 620x1348
pixels; their CSS viewport was not recorded. The implemented mobile steps
use 390x848 CSS pixels at scale 62/39 to match that output size. They show
Chromium mobile emulation, not installed iOS or operating-system chrome.

Brand icons and architecture illustrations, including
`ui/static/brand/cognis-mark-color.svg` and
`docs/assets/images/cognis-cloud-native-agent-os.svg`, are not screenshots
and are outside this refresh.

With `SHOWCASE_MANIFEST`, `SHOWCASE_PRIVATE_ENV`, and `SHOWCASE_OUTPUT` set as
above, refresh the implemented desktop views with:

```sh
SHOWCASE_SCREENSHOTS_ONLY=1 make validate COMMAND="ui/node_modules/.bin/playwright test --config tests/e2e/showcase/playwright.config.ts --project desktop-docs"
```

After inspecting the Work diff PNG and preserving the original, replace its
existing path. The Rich previews emitted by this project are not the canonical
per-block documentation assets; refresh those with their owner below.

```sh
ffmpeg -nostdin -y -i "$SHOWCASE_OUTPUT/desktop-docs/desktop-work-diff.png" -frames:v 1 -c:v libwebp -lossless 1 -compression_level 6 -pix_fmt bgra docs/assets/screenshots/desktop-work-diff.webp
```

The existing Rich documentation owner captures the individual block previews
listed in `docs/assets/screenshots/rich-deliverables/manifest.json`. Its
per-preview Playwright test names are recorded in the inventory. Preserve the
existing image directory first, then run its screenshot update against this
same isolated fixture:

If that task-owned runtime predates the preview source, update only its static UI
from this checkout before capturing. Do not apply this to a production or shared
runtime. Resolve the UI directory through its existing owner, preserve the old
build, and copy the new build into that directory; no backend restart is needed.
The container image identity remains unchanged by this frontend-only overlay.

```sh
CAPTURE_CONTAINER="$(docker compose --project-name "$PROJECT" --env-file "$RUNTIME/compose.env" -f compose.local.yml -f compose.e2e.yml -f tests/e2e/showcase/compose.yml ps -q cognis)"
test -n "$CAPTURE_CONTAINER"
test "$(docker inspect --format '{{index .Config.Labels "com.docker.compose.project"}}' "$CAPTURE_CONTAINER")" = "$PROJECT"
UI_DIR="$(docker exec "$CAPTURE_CONTAINER" python -c 'from cognis.ui_assets import resolve_ui_build_dir; p=resolve_ui_build_dir(); assert p is not None; print(p)')"
UI_BACKUP="$(mktemp -d /tmp/showcase-ui-backup.XXXXXXXX)"
docker cp "$CAPTURE_CONTAINER:$UI_DIR" "$UI_BACKUP/previous-build"
make validate COMMAND="npm --prefix ui run build"
docker cp ui/build/. "$CAPTURE_CONTAINER:$UI_DIR/"
```

```sh
COGNIS_E2E_URL="$(python3 -c 'import json,os; print(json.load(open(os.environ["SHOWCASE_MANIFEST"]))["base_url"])')" SHOWCASE_SCREENSHOTS_ONLY=1 UPDATE_RICH_DOC_ASSETS=1 make validate COMMAND="bash -c 'cd ui && node_modules/.bin/playwright test --config playwright.config.ts e2e/rich-deliverable-doc-assets.spec.ts --grep \"documentation preview|compact gallery toolbar\" --grep-invert \"documentation preview chart matches\" --workers=1 --retries=0 --max-failures=1'"
```

The excluded generic `chart.png` has no pre-PR documentation reference. Inspect
each generated screenshot before retaining it. The figure preview uses a taller
viewport to render the complete image. The current visual-card preview is
letterboxed rather than full-bleed; this is an observed rendering limitation.
The card-visual test checks the actually served sample text before writing an
image, so an old built preview cannot silently pass an update. To refresh only
that asset, use the same command with
`--grep "documentation preview card-visual matches"` and without `--grep-invert`.

The populated skills registry and executor tabs have separate named captures:

```sh
SHOWCASE_SCREENSHOTS_ONLY=1 make validate COMMAND="ui/node_modules/.bin/playwright test --config tests/e2e/showcase/playwright.config.ts --project tools-docs"
SHOWCASE_SCREENSHOTS_ONLY=1 make validate COMMAND="ui/node_modules/.bin/playwright test --config tests/e2e/showcase/playwright.config.ts --project executor-docs"
```

After inspecting the output and preserving the original, replace its existing
path:

```sh
ffmpeg -nostdin -y -i "$SHOWCASE_OUTPUT/tools-docs/tools-skills.png" -frames:v 1 -c:v libwebp -lossless 1 -compression_level 6 -pix_fmt bgra docs/assets/screenshots/tools-skills.webp
ffmpeg -nostdin -y -i "$SHOWCASE_OUTPUT/executor-docs/executor-browser.png" -frames:v 1 -c:v libwebp -lossless 1 -compression_level 6 -pix_fmt bgra docs/assets/screenshots/executor-browser-tools.webp
cp "$SHOWCASE_OUTPUT/executor-docs/executor-tools.png" docs/assets/screenshots/executor-tools.png
```

Capture the `Launch readiness brief` task, which the seed completed through the
normal general-task workflow with scripted local mock output:

```sh
SHOWCASE_SCREENSHOTS_ONLY=1 make validate COMMAND="ui/node_modules/.bin/playwright test --config tests/e2e/showcase/playwright.config.ts --project task-docs"
```

After inspecting both captures and preserving the original, replace its path
and add the task brief:

```sh
ffmpeg -nostdin -y -i "$SHOWCASE_OUTPUT/task-docs/mobile-completed-task.png" -frames:v 1 -c:v libwebp -lossless 1 -compression_level 6 -pix_fmt bgra docs/assets/screenshots/pwa-task.webp
cp "$SHOWCASE_OUTPUT/task-docs/mobile-task-brief.png" docs/assets/screenshots/mobile-task-brief.png
```

The command views use the seeded `git diff --stat` call in the saved-carts
conversation, and the research chat uses the seeded `Brief: AI shopping
assistants` conversation with `web_search` and `web_fetch` history on reserved
example domains. Nothing was executed and no web request was made:

```sh
SHOWCASE_SCREENSHOTS_ONLY=1 make validate COMMAND="ui/node_modules/.bin/playwright test --config tests/e2e/showcase/playwright.config.ts --project command-docs"
```

After inspecting both screenshots and preserving the originals:

```sh
ffmpeg -nostdin -y -i "$SHOWCASE_OUTPUT/command-docs/desktop-work-commands.png" -frames:v 1 -c:v libwebp -lossless 1 -compression_level 6 -pix_fmt bgra docs/assets/screenshots/desktop-work-commands.webp
ffmpeg -nostdin -y -i "$SHOWCASE_OUTPUT/command-docs/mobile-command-output.png" -frames:v 1 -c:v libwebp -lossless 1 -compression_level 6 -pix_fmt bgra docs/assets/screenshots/mobile-command-output.webp
```

The overview capture uses the same saved-carts conversation. If it has no
context estimate yet, it registers `showcase-review-ready-v1` on the local mock
server and asks one ordinary review question. That real local-mock turn
produces a native context estimate without live inference. The overview and
research steps reject diagnostic markers in the published conversations.

```sh
SHOWCASE_SCREENSHOTS_ONLY=1 make validate COMMAND="ui/node_modules/.bin/playwright test --config tests/e2e/showcase/playwright.config.ts --project overview-docs"
```

After inspecting the screenshots and preserving the originals:

```sh
ffmpeg -nostdin -y -i "$SHOWCASE_OUTPUT/overview-docs/desktop-workspace.png" -frames:v 1 -c:v libwebp -lossless 1 -compression_level 6 -pix_fmt bgra docs/assets/screenshots/desktop-workspace.webp
ffmpeg -nostdin -y -i "$SHOWCASE_OUTPUT/overview-docs/mobile-activity-overview.png" -frames:v 1 -c:v libwebp -lossless 1 -compression_level 6 -pix_fmt bgra docs/assets/screenshots/mobile-activity-overview.webp
cp "$SHOWCASE_OUTPUT/overview-docs/mobile-activity-recent.png" docs/assets/screenshots/mobile-activity-recent.png
```

Capture the dashboard workspace windows using the saved fixture document:

```sh
SHOWCASE_SCREENSHOTS_ONLY=1 make validate COMMAND="ui/node_modules/.bin/playwright test --config tests/e2e/showcase/playwright.config.ts --project windows-docs"
```

After inspecting both screenshots and preserving the originals:

```sh
ffmpeg -nostdin -y -i "$SHOWCASE_OUTPUT/windows-docs/desktop-window-activity.png" -frames:v 1 -c:v libwebp -lossless 1 -compression_level 6 -pix_fmt bgra docs/assets/screenshots/desktop-window-activity.webp
ffmpeg -nostdin -y -i "$SHOWCASE_OUTPUT/windows-docs/desktop-window-deliverable.png" -frames:v 1 -c:v libwebp -lossless 1 -compression_level 6 -pix_fmt bgra docs/assets/screenshots/desktop-window-deliverable.webp
```

Refresh the implemented mobile views with:

```sh
SHOWCASE_SCREENSHOTS_ONLY=1 make validate COMMAND="ui/node_modules/.bin/playwright test --config tests/e2e/showcase/playwright.config.ts --project mobile-docs"
SHOWCASE_SCREENSHOTS_ONLY=1 make validate COMMAND="ui/node_modules/.bin/playwright test --config tests/e2e/showcase/playwright.config.ts --project active-docs"
SHOWCASE_SCREENSHOTS_ONLY=1 make validate COMMAND="ui/node_modules/.bin/playwright test --config tests/e2e/showcase/playwright.config.ts --project ongoing-docs"
```

The active-list step creates a separate dedicated activity conversation, starts
one real turn against the local mock model there, and waits for it to finish.
It never sends probe controls to the published conversations. Inspect all
four PNGs under `$SHOWCASE_OUTPUT/mobile-docs/`
and the active-list PNG before replacing the existing documentation files.
The `Mobile active session overview` step registers the bounded
`showcase-ongoing-text-v2` text-only response on the existing local mock server
and captures a fresh conversation while both the API active-turn flag and its
native execution-session row indicate running. It waits for completion afterward.
This is the current Overview equivalent of the former three-session demo, not a
claim of three parallel children. Inspect its PNG under
`$SHOWCASE_OUTPUT/ongoing-docs/` too. All these mobile captures use 390x848 CSS
pixels at scale 62/39; no command or executor journal operation is involved.
Preserve the originals, then write the inspected images in their existing format
and paths:

```sh
ffmpeg -nostdin -y -i "$SHOWCASE_OUTPUT/mobile-docs/mobile-chat.png" -frames:v 1 -c:v libwebp -lossless 1 -compression_level 6 -pix_fmt bgra docs/assets/screenshots/pwa-chat.webp
ffmpeg -nostdin -y -i "$SHOWCASE_OUTPUT/mobile-docs/mobile-conversation-drawer.png" -frames:v 1 -c:v libwebp -lossless 1 -compression_level 6 -pix_fmt bgra docs/assets/screenshots/pwa-conversations.webp
ffmpeg -nostdin -y -i "$SHOWCASE_OUTPUT/mobile-docs/mobile-work-files.png" -frames:v 1 -c:v libwebp -lossless 1 -compression_level 6 -pix_fmt bgra docs/assets/screenshots/mobile-work-files.webp
ffmpeg -nostdin -y -i "$SHOWCASE_OUTPUT/mobile-docs/mobile-work-diff.png" -frames:v 1 -c:v libwebp -lossless 1 -compression_level 6 -pix_fmt bgra docs/assets/screenshots/mobile-work-diff.webp
ffmpeg -nostdin -y -i "$SHOWCASE_OUTPUT/active-docs/mobile-conversations.png" -frames:v 1 -c:v libwebp -lossless 1 -compression_level 6 -pix_fmt bgra docs/assets/screenshots/mobile-conversations.webp
ffmpeg -nostdin -y -i "$SHOWCASE_OUTPUT/ongoing-docs/mobile-ongoing-work.png" -frames:v 1 -c:v libwebp -lossless 1 -compression_level 6 -pix_fmt bgra docs/assets/screenshots/mobile-ongoing-work.webp
```

After the active mock turn has completed, capture the research chat. This
ensures the normal Chat v2 snapshot includes the recorded fixture events:

```sh
SHOWCASE_SCREENSHOTS_ONLY=1 make validate COMMAND="ui/node_modules/.bin/playwright test --config tests/e2e/showcase/playwright.config.ts --project research-docs"
ffmpeg -nostdin -y -i "$SHOWCASE_OUTPUT/research-docs/chat-desktop.png" -frames:v 1 -c:v libwebp -lossless 1 -compression_level 6 -pix_fmt bgra docs/assets/screenshots/chat-desktop.webp
```

The generic supplemental paths come from the named comparison and feature steps
above, not renamed feature substitutes for the 66 original views. After inspecting
those captures, install them at the paths recorded in the inventory:

```sh
for FAMILY in classic porcelain paper; do
  cp "$SHOWCASE_OUTPUT/comparison/$FAMILY-usage.png" "docs/assets/screenshots/appearance/$FAMILY-usage.png"
  cp "$SHOWCASE_OUTPUT/comparison/$FAMILY-dark-usage.png" "docs/assets/screenshots/appearance/$FAMILY-dark-usage.png"
done
cp "$SHOWCASE_OUTPUT/comparison/porcelain-rich-chart.png" docs/assets/screenshots/rich-chart-overview.png
cp "$SHOWCASE_OUTPUT/feature/04-tests-diff.png" docs/assets/screenshots/work-file-navigation.png
cp "$SHOWCASE_OUTPUT/feature/04-diff.png" docs/assets/screenshots/work-expanded-diff.png
cp "$SHOWCASE_OUTPUT/feature/06-rich.png" docs/assets/screenshots/rich-overview.png
cp "$SHOWCASE_OUTPUT/feature/08-html.png" docs/assets/screenshots/interactive-html.png
cp "$SHOWCASE_OUTPUT/feature/09-project.png" docs/assets/screenshots/project-members.png
cp "$SHOWCASE_OUTPUT/feature/10-sharing.png" docs/assets/screenshots/document-sharing.png
cp "$SHOWCASE_OUTPUT/feature/11-comments.png" docs/assets/screenshots/document-comments.png
cp "$SHOWCASE_OUTPUT/feature/11-source.png" docs/assets/screenshots/document-comment-source.png
cp "$SHOWCASE_OUTPUT/feature/11-revisions.png" docs/assets/screenshots/document-revisions.png
```

### Hero views and GIFs

The hero project captures the README's main views and four GIFs: the
dashboard, command palette, project dashboard, Usage overview, the Usage cost
flow, pinned chats, opening and switching dashboard windows, drilling from the
most expensive day into the cost flow, commenting on a Rich result and sending
the feedback to the agent, and opening pinned agent chats from the Library.
GIF frames come from a Chromium screencast of the real UI; the only addition is
a presentation cursor that moves in eased, display-rate steps and is removed
after recording. `ffmpeg` builds 960 px, 25 fps GIFs with one shared palette.
The pinned-chats step pins three conversations from their chat headers. The
macOS app's desktop-wide pins are described in the README but not captured
here; they need the native app and the host desktop.

The feedback GIF adds one comment and one real local-mock turn to the seeded
`Autumn invitation feedback` conversation; the agent answers the comment with
the real `comment_reply` tool. Run it once per fresh runtime. A repeat run adds
another comment and turn, so start a fresh runtime before recapturing it.
Usage views end yesterday because live local-mock turns report no token usage
and would otherwise appear unpriced.

```sh
SHOWCASE_SCREENSHOTS_ONLY=1 make validate COMMAND="ui/node_modules/.bin/playwright test --config tests/e2e/showcase/playwright.config.ts --project hero-docs"
for VIEW in dashboard command-palette project-dashboard usage-overview usage-flow pinned-chats; do
  ffmpeg -nostdin -y -i "$SHOWCASE_OUTPUT/hero-docs/$VIEW.png" -frames:v 1 -c:v libwebp -quality 92 -compression_level 6 "docs/assets/screenshots/$VIEW.webp"
done
for GIF in dashboard-windows usage-drilldown work-feedback pinned-chats; do
  cp "$SHOWCASE_OUTPUT/hero-docs/$GIF.gif" "docs/assets/screenshots/$GIF.gif"
done
```

To validate a completed instance again without rerunning the local-provider
bootstrap, invoke the same seed directly:

```sh
make validate COMMAND="docker compose --project-name $PROJECT --env-file $RUNTIME/compose.env -f compose.local.yml -f compose.e2e.yml -f tests/e2e/showcase/compose.yml run --rm --entrypoint python showcase-seed /showcase/seed.py"
```

### UI sweep captures

`sweep.spec.ts` captures the same views at desktop (1440×900) and phone
(iPhone 13) size, one test per UI-sweep batch, so later sweeps compare like for
like. On first run it adds three outcome tasks through the local mock model: a
task paused on a step question, a failed task and a draft. It refuses
non-loopback base URLs.

```sh
SWEEP_LABEL=after make validate COMMAND='bash -c "cd tests/e2e/showcase && ../../../ui/node_modules/.bin/playwright test --config playwright.config.ts --project sweep-desktop --project sweep-phone"'
```

Screenshots land in `$SHOWCASE_OUTPUT/sweep/$SWEEP_LABEL/<project>/<batch>-<view>.png`.
To capture another UI build against the same runtime (for example the base
branch for before/after pairs), serve that build with `/api` proxied to the
runtime and set `SWEEP_BASE_URL` to it. The `UI sweep question answer dialog`
test answers the seeded question once per runtime and records the panel before
and after.

### Film scenes

`film/capture.mjs` records product scenes as 1920x1080 MP4 clips: 1280x720 CSS at device
scale 1.5, with a smooth cursor composited afterwards at 30 fps. It needs
`ffmpeg` on `PATH`. Use a fresh runtime: the `feedback` scene sends a message,
and `durable` creates the `Staging watch` workflow from
`film/staging-watch-workflow.json`.

```sh
RUNTIME="$RUNTIME" node tests/e2e/showcase/film/capture.mjs dashboard pinned feedback durable library work usage models
```

Clips land in `tests/e2e/showcase/film/output/` (ignored) or `$FILM_OUT`.
Editing, narration and music are outside this repository.

## Helper map

- `prepare.py`: create a private runtime and attest running image identities.
- `compose.yml`: isolate ports, volumes, token directory and seed wiring.
- `seed.py`: own core records, event evidence, completion marker and manifest.
- `workspace.py`: the agent crew and avatars, extra projects, seeded
  conversations, schedules and mock-completed tasks.
- `content.py`, `usage.py`, `feedback.py`: create their canonical records once;
  their input bytes are included in the fixture digest.
- `capture.spec.ts` and `playwright.config.ts`: browser capture and checkpoints.
- `sweep.spec.ts`: per-batch UI sweep captures at desktop and phone size.
- `tsconfig.json`: the capture's Node/Playwright type-check boundary.
- `film/`: film scene capture and the demo workflow it creates.

## Teardown and fresh restore

Remove this run's containers, volumes, networks and session-only images without
touching other projects or shared service images:

```sh
docker compose --project-name "$PROJECT" --env-file "$RUNTIME/compose.env" -f compose.local.yml -f compose.e2e.yml -f tests/e2e/showcase/compose.yml down -v --remove-orphans --rmi local
docker image rm "${PROJECT}-controller:local" "${PROJECT}-executor:local"
```

`--rmi local` removes the project's untagged build images, including mock-llm;
the explicit removal covers this run's custom controller/executor tags.
If an image is still in use, investigate its owner; never force its removal.

For a clean second restore, first make the reviewed fixture available in an
immutable Git revision. Set `SOURCE_REPOSITORY` to the authorized repository
URL and `SOURCE_REVISION` to that revision, then use a fresh path and fresh
dependency caches:

```sh
export RESTORE_ROOT="$(mktemp -d /tmp/lumen-restore.XXXXXXXX)"
git clone --no-local "$SOURCE_REPOSITORY" "$RESTORE_ROOT/checkout"
cd "$RESTORE_ROOT/checkout"
git checkout --detach "$SOURCE_REVISION"
export npm_config_cache="$RESTORE_ROOT/npm-cache"
export PLAYWRIGHT_BROWSERS_PATH="$RESTORE_ROOT/playwright-browsers"
```

Repeat **Fresh runtime** and install dependencies as above from this clone. This
creates a different runtime path, project, ports, volumes and credentials; it
does not depend on the old checkout, credentials, caches or a database export.
Do not remove any volumes without explicit intent. A successful restore is not
claimed until the fresh run and its repeat-seed validation have been observed.
