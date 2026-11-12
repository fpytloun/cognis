import { defineConfig, devices } from '@playwright/test';

export default defineConfig({
  testDir: './e2e', testMatch: 'previews.browser.ts', workers: 1, timeout: 30_000,
  use: { ...devices['Desktop Chrome'], baseURL: 'http://127.0.0.1:43131' },
  webServer: {
    command: 'node scripts/build-artifact-preview-fixture.mjs ../.local/web-preview-fixture --web && python3 -m http.server 43131 --bind 127.0.0.1 --directory ../.local/web-preview-fixture',
    url: 'http://127.0.0.1:43131', timeout: 120_000, reuseExistingServer: false,
  },
});
