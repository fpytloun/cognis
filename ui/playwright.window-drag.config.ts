import { defineConfig } from '@playwright/test';

export default defineConfig({
  testDir: './e2e',
  testMatch: 'dashboard-window-drag.spec.ts',
  workers: 1,
  retries: 0,
  timeout: 60_000,
  outputDir: process.env.DRAG_DIAGNOSTIC === '1'
    ? process.env.DRAG_ORIGINAL_SCOPE === '1' ? '.window-drag-static5-diagnostic-results' : '.window-drag-diagnostic-results'
    : process.env.DRAG_EXPANSION === '1' ? '.window-drag-expansion-results' : '.window-drag-results',
  use: {
    baseURL: 'http://127.0.0.1:4178', viewport: { width: 1540, height: 980 },
    trace: process.env.DRAG_DIAGNOSTIC === '1' ? 'retain-on-failure' : 'off',
    serviceWorkers: process.env.DRAG_DIAGNOSTIC === '1' ? 'block' : 'allow',
  },
  projects: [{ name: 'chromium', use: { browserName: 'chromium' } }],
  webServer: {
    command: './node_modules/.bin/vite build --config vite.window-drag.config.ts && ./node_modules/.bin/vite preview --config vite.window-drag.config.ts --host 127.0.0.1 --port 4178 --strictPort',
    url: 'http://127.0.0.1:4178',
    reuseExistingServer: false,
  },
});
