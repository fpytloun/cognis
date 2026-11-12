import { defineConfig } from '@playwright/test';
import base from './playwright.config';

export default defineConfig({
  ...base,
  testMatch: ['library-whole-comment.spec.ts', 'public-guest-comment.spec.ts', 'document-conversation.spec.ts', 'document-search.spec.ts'],
  testIgnore: [],
  grepInvert: undefined,
  retries: 0,
  // Static preview uses intercepted API responses; this is not deployment CSP coverage.
  use: { ...base.use, baseURL: 'http://127.0.0.1:4218', serviceWorkers: 'block', bypassCSP: true },
  webServer: {
    command: 'npx vite preview --config vite.spa-preview.config.mjs --host 127.0.0.1 --port 4218 --strictPort',
    url: 'http://127.0.0.1:4218',
    reuseExistingServer: false,
  },
  projects: [
    { name: 'chromium', use: { browserName: 'chromium' } },
    { name: 'webkit', use: { browserName: 'webkit' } },
  ],
});
