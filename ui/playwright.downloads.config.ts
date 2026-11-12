import { defineConfig, devices } from '@playwright/test';

export default defineConfig({
  testDir: './e2e', testMatch: 'downloads.browser.ts', workers: 1, timeout: 30_000,
  use: { ...devices['Desktop Chrome'], baseURL: 'http://127.0.0.1:43128' },
  webServer: {
    command: 'node scripts/build-download-fixture.mjs ../.local/download-fixture && node scripts/serve-download-fixture.mjs ../.local/download-fixture 43128',
    url: 'http://127.0.0.1:43128', timeout: 120_000, reuseExistingServer: false,
  },
});
