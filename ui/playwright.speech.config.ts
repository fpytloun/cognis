import { defineConfig, devices } from '@playwright/test';

/** Local-only real-component fixture; no credentials or backend/provider calls. */
export default defineConfig({
  testDir: './e2e',
  testMatch: 'speech-playback.browser.ts',
  workers: 1,
  timeout: 60_000,
  use: { ...devices['Desktop Chrome'], baseURL: 'http://127.0.0.1:43127',
    trace: 'retain-on-failure', screenshot: 'only-on-failure' },
  webServer: {
    command: 'npm run dev -- --config vite.speech.config.ts --host 127.0.0.1 --port 43127 --strictPort',
    url: 'http://127.0.0.1:43127/speech-playback-browser-fixture',
    timeout: 120_000,
    reuseExistingServer: false
  }
});
