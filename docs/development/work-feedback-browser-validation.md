# Work feedback browser validation

Run source-first UI checks in the task-owned worktree. Do not run SvelteKit
sync, Vitest, production builds or other generated-route writers concurrently
with a live Vite browser acceptance run: route regeneration can reload the page.

The focused matrix is:

```sh
COGNIS_E2E_URL=http://127.0.0.1:5198 npx playwright test --config playwright.feedback.config.ts --workers=1 --retries=0
```

It covers native mouse selection across existing pins, compact per-message
ownership, retained Work focus/drafts, and ResourceReader with real bottom
navigation and a long comments list. The latter records 25 animation frames for
the header, Close action and docked Review action, then closes/reopens normally.
Trace JSON and screenshots are saved under the configured test output directory.
An authentication precondition failure is **blocked**, not a passing layout
check. Never bypass document coordination or skip the layout assertions.

## Actual-app and native touch gate

Before acceptance, use the actual application with normal login in Chromium
and WebKit at 390×844 with touch/mobile enabled. Repeat text Save → Close preview
→ Comments. Record the same rectangles over at least 25 frames and verify normal
Close comments and reopen without forced clicks.

Native touch-handle selection requires a separate device/browser check; taps and
programmatically assigned DOM ranges do not prove it. On Safari/iOS and
Chrome/Android, or an equivalent browser-native touch harness:

1. Save three overlapping comments on `Prepare the staging deployment plan.`
   after an emoji, leaving the full surrounding source visible.
2. Long-press annotated text, extend both native selection handles through the
   entire quote in both directions, and verify that no saved pin captures the
   gesture or extends selection into metadata.
3. Add/save once; verify the complete quote and exact occurrence on reopen.
4. Activate each saved pin normally, and verify its 44px control remains
   reachable. Recheck at maximum source scroll and with the keyboard open.

Record device, engine, native gesture method and outcome. Until that evidence is
available, report native touch-handle selection as **not verified** rather than
inferring it from desktop drags or region-drawing touch coverage.
