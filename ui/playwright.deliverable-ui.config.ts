import { defineConfig } from '@playwright/test';

export default defineConfig({
  testDir: './e2e', testMatch: 'deliverable-ui.spec.ts',
  workers: 1, fullyParallel: false, retries: 0,
  use: { baseURL: 'http://127.0.0.1:4179', browserName: 'chromium' },
  webServer: {
    command: 'npm exec vite -- --config vite.deliverable-ui.config.ts --host 127.0.0.1 --port 4179 --strictPort',
    url: 'http://127.0.0.1:4179/__deliverable-fixture',
    reuseExistingServer: false,
  },
});
