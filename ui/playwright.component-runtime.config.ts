import { defineConfig, devices } from '@playwright/test';
import { COMPONENT_RUNTIME_ORIGIN } from './e2e/component-runtime';

const fixture = new URL(COMPONENT_RUNTIME_ORIGIN);

export default defineConfig({
  testDir: './e2e',
  testMatch: ['brand.spec.ts', 'chat-motion.spec.ts', 'streaming.spec.ts',
    'scoped-chat-v2.spec.ts', 'chat-appearance.spec.ts', 'chat-message-width.spec.ts',
    'scoped-history-anchor.spec.ts', 'timeline-scroll-stability.spec.ts',
    'session-logs-drawer.spec.ts'],
  timeout: 60_000,
  expect: { timeout: 10_000 },
  fullyParallel: false,
  workers: 1,
  retries: 0,
  reporter: [['html', { open: 'never' }], ['list']],
  webServer: {
    command: `./node_modules/.bin/vite build --config vite.component-runtime.config.ts && ./node_modules/.bin/vite preview --config vite.component-runtime.config.ts --host ${fixture.hostname} --port ${fixture.port} --strictPort`,
    url: `${COMPONENT_RUNTIME_ORIGIN}/streaming.html`,
    reuseExistingServer: false,
  },
  use: {
    baseURL: process.env.COGNIS_E2E_URL ?? 'http://localhost:8080',
    trace: 'on-first-retry',
    screenshot: 'only-on-failure',
    video: 'retain-on-failure',
  },
  projects: [{ name: 'chromium', use: { ...devices['Desktop Chrome'] } }],
});
