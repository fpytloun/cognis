import { defineConfig } from '@playwright/test';

export default defineConfig({
  testDir: './e2e',
  testMatch: ['unified-chat-workspace.spec.ts', 'ongoing-work-drawer.spec.ts'],
  workers: 1,
  retries: 0,
  timeout: 60_000,
  outputDir: '.chat-workspace-results',
  use: {
    baseURL: 'http://127.0.0.1:4197',
    viewport: { width: 1440, height: 1000 },
    serviceWorkers: 'block',
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
  },
  projects: [{ name: 'chromium', use: { browserName: 'chromium' } }],
  webServer: {
    command: './node_modules/.bin/vite --config vite.chat-workspace.config.ts --host 127.0.0.1 --port 4197 --strictPort',
    url: 'http://127.0.0.1:4197',
    reuseExistingServer: false,
  },
});
