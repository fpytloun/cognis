import { defineConfig, devices } from '@playwright/test';

export default defineConfig({
  testDir: './e2e',
  testMatch: ['timeline-client-diagnostic.spec.ts', 'timeline-history.spec.ts', 'chat-loading-audit.spec.ts', 'main-chat-history-scroll.spec.ts', 'carried-assistant-ordering.spec.ts', 'failed-admission-ordering.spec.ts', 'timeline-force-refresh.spec.ts'],
  timeout: 60_000,
  expect: { timeout: 10_000 },
  workers: 1,
  retries: 0,
  reporter: 'list',
  use: { baseURL: 'http://127.0.0.1:4193', screenshot: 'only-on-failure' },
  webServer: {
    command: 'npx vite build --config vite.timeline-client.config.ts && npx vite preview --config vite.timeline-client.config.ts --host 127.0.0.1 --port 4193 --strictPort',
    url: 'http://127.0.0.1:4193',
    timeout: 180_000,
    reuseExistingServer: false,
  },
  projects: [
    { name: 'chromium', use: { ...devices['Desktop Chrome'] } },
    { name: 'webkit', use: { ...devices['iPhone 13'] } },
  ],
});
