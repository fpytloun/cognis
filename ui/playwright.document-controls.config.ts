import { defineConfig } from '@playwright/test';
import base from './playwright.config';

// Fixture-backed controls need no live API. Point COGNIS_E2E_URL at the UI.
export default defineConfig({
  ...base,
  // Service-worker fetches bypass Playwright's mock routes.
  use: { ...base.use, serviceWorkers: 'block' },
  testMatch: ['document-controls.spec.ts', 'chat-deliverable-flow.spec.ts'],
  projects: [
    { name: 'chromium', use: { browserName: 'chromium' } },
    { name: 'webkit', use: { browserName: 'webkit' } },
  ],
});
