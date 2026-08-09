# Stage 41 screenshot evidence

- Base commit: `81b214344e8d`
- UI source identity: `81b214344e8d+332c81f20d1d`
- Build command: `VITE_STAGE41_SOURCE_TREE="$SOURCE_ID" npm --prefix ui run build`
- Server command: `VITE_STAGE41_SOURCE_TREE="$SOURCE_ID" npm run dev -- --host 127.0.0.1 --port 5175`
- Browser command: `STAGE41_SOURCE_TREE="$SOURCE_ID" COGNIS_E2E_URL=http://127.0.0.1:5175 npx playwright test e2e/task-cockpit.spec.ts e2e/task-cockpit-stage41.spec.ts --project=chromium --workers=1 --retries=0`
- Result: 10 passed, 0 failed, 0 retries

The browser suite checks the source identity from the rendered task page.

Visual inspection confirmed:

- `task-cockpit-desktop-paused-overview.png` and `task-cockpit-mobile-paused-overview.png` show `Review decision`.
- `workflow-builder-deterministic-step.png` shows only the selected Tools group and labels the step as `TOOL CALL`.
- `task-cockpit-desktop-control-chat.png` and `task-cockpit-mobile-control-chat-reopened.png` show the native scoped timeline without a nested app shell.
- `task-cockpit-mobile-control-chat-reopened.png` keeps the composer visible above the safe-area edge.
