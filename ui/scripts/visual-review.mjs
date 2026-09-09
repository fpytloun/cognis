// Ad-hoc visual review helper (not part of the test suite).
// Screenshots rich-deliverable fixture routes across themes/viewports for design iteration.
// Usage: node scripts/visual-review.mjs [outputDir] [tag]
import { chromium } from '@playwright/test';
import { mkdir } from 'node:fs/promises';
import path from 'node:path';

const BASE_URL = process.env.VISUAL_REVIEW_BASE_URL ?? 'http://localhost:5173';
const outDir = process.argv[2] ?? '/tmp/rich-visual-review';
const tag = process.argv[3] ?? 'run';

const targets = [
  { route: '/rich-deliverable-pulse-fixture', name: 'pulse-embedded' },
  { route: '/rich-deliverable-prototype-fixture', name: 'prototype-390' },
  { route: '/rich-deliverable-fixture', scenario: 'research-answer', name: 'research-answer' },
  { route: '/rich-deliverable-fixture', scenario: 'product-comparison', name: 'product-comparison' },
  { route: '/rich-deliverable-fixture', scenario: 'metrics-dashboard', name: 'metrics-dashboard' },
  { route: '/rich-deliverable-fixture', scenario: 'incident-report', name: 'incident-report' },
  { route: '/rich-deliverable-fixture', scenario: 'newsletter-digest', name: 'newsletter-digest' },
  { route: '/rich-deliverable-fixture', scenario: 'freeform-notes', name: 'freeform-notes' },
  { route: '/rich-deliverable-fixture', scenario: 'visual-system-reference', name: 'visual-system-reference' },
  { route: '/rich-deliverable-fixture', scenario: 'daily-pulse-v2', name: 'daily-pulse-standalone' },
];

const themes = ['dark', 'light'];
const viewports = [
  { name: 'desktop', width: 1280, height: 1600 },
  { name: 'mobile', width: 390, height: 1400 },
];

const runDir = path.join(outDir, tag);
await mkdir(runDir, { recursive: true });

const browser = await chromium.launch();
for (const target of targets) {
  for (const theme of themes) {
    for (const viewport of viewports) {
      const context = await browser.newContext({
        viewport: { width: viewport.width, height: viewport.height },
        colorScheme: theme,
      });
      const page = await context.newPage();
      try {
        const query = target.scenario
          ? `?${new URLSearchParams({ scenario: target.scenario, theme, width: String(viewport.width), surface: 'embedded' })}`
          : '';
        await page.goto(`${BASE_URL}${target.route}${query}`, { waitUntil: 'networkidle', timeout: 15000 });
        await page.waitForTimeout(500);
        const filename = `${target.name}--${theme}--${viewport.name}.png`;
        await page.screenshot({ path: path.join(runDir, filename), fullPage: true });
        console.log(`OK ${filename}`);
      } catch (error) {
        console.error(`FAIL ${target.route} ${theme} ${viewport.name}:`, error.message);
      } finally {
        await context.close();
      }
    }
  }
}
await browser.close();
console.log(`Screenshots written to ${runDir}`);
