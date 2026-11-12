import { defineConfig, devices } from '@playwright/test';

// Run after npm run build to exercise the compiled exporter and Session disclosure.
export default defineConfig({
  testDir: './e2e',
  testMatch: 'timeline-diagnostic-export.spec.ts',
  workers: 1,
  retries: 0,
  use: { baseURL: 'http://127.0.0.1:4198', trace: 'retain-on-failure' },
  webServer: {
    command: 'npx vite preview --config vite.spa-preview.config.mjs --host 127.0.0.1 --port 4198 --strictPort',
    url: 'http://127.0.0.1:4198',
    reuseExistingServer: false,
  },
  projects: [
    { name: 'chromium', use: { ...devices['Desktop Chrome'] } },
    { name: 'webkit', use: { ...devices['iPhone 13'] } },
  ],
});
