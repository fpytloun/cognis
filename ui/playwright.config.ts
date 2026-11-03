import { defineConfig, devices } from '@playwright/test';
import { readFileSync } from 'node:fs';

const harnesses: { specs?: string[]; grep?: string }[] = JSON.parse(
  readFileSync(new URL('./browser-harnesses.json', import.meta.url), 'utf8')
);
const routed = harnesses.filter((harness) => harness.specs);
const escapePattern = (value: string) => value.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');

/**
 * Playwright configuration for L3 browser e2e tests.
 *
 * Requires the e2e compose stack to be running:
 *   make e2e-up && make e2e-seed
 *
 * Run:
 *   make e2e-browser  (all owning harnesses and devices)
 *   cd ui && npx playwright test --ui  (compose-owned cases only)
 */
export default defineConfig({
  testDir: './e2e',
  testIgnore: routed.filter((harness) => !harness.grep).flatMap((harness) =>
    harness.specs!.map((spec) => `**/${spec}`)),
  grepInvert: new RegExp(routed.filter((harness) => harness.grep).flatMap((harness) =>
    harness.specs!.map((spec) => `${escapePattern(spec)}.*(?:${harness.grep})`)).join('|')),
  timeout: 60_000,
  expect: { timeout: 10_000 },
  fullyParallel: false,  // Sequential — shared compose stack
  workers: 1,
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
