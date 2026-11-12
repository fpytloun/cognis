import { defineConfig, devices } from '@playwright/test';
import base from './playwright.config';

export default defineConfig({
  ...base,
  testMatch: ['work-feedback-table-range.spec.ts', 'work-feedback-location.spec.ts'],
  retries: 0,
  reporter: 'list',
  use: { ...base.use, baseURL: 'http://127.0.0.1:6127', colorScheme: 'dark' },
  webServer: {
    command: 'npm run dev -- --config e2e/fixtures/table-range/vite.config.ts --host 127.0.0.1 --port 6127 --strictPort',
    url: 'http://127.0.0.1:6127/e2e/fixtures/table-range/index.html',
    reuseExistingServer: false,
  },
  projects: [
    { name: 'chromium', use: { ...devices['Desktop Chrome'] } },
    { name: 'webkit', use: { ...devices['Desktop Safari'] } },
  ],
});
