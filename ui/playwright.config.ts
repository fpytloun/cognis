import { defineConfig, devices } from '@playwright/test';

/**
 * Playwright configuration for L3 browser e2e tests.
 *
 * Requires the e2e compose stack to be running:
 *   make e2e-up && make e2e-seed
 *
 * Run:
 *   cd ui && npx playwright test e2e/
 *   cd ui && npx playwright test e2e/ --ui  (interactive)
 */
export default defineConfig({
  testDir: './e2e',
  timeout: 60_000,
  expect: { timeout: 10_000 },
  fullyParallel: false,  // Sequential — shared compose stack
  retries: 1,
  reporter: [['html', { open: 'never' }], ['list']],

  use: {
    baseURL: process.env.COGNIS_E2E_URL ?? 'http://localhost:8080',
    trace: 'on-first-retry',
    screenshot: 'only-on-failure',
    video: 'retain-on-failure',
  },

  projects: [
    {
      name: 'chromium',
      use: { ...devices['Desktop Chrome'] },
    },
  ],
});
