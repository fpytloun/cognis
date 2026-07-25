// Ad-hoc visual review helper (not part of the test suite).
// Screenshots rich-deliverable fixture routes across themes/viewports for design iteration.
// Usage: node scripts/visual-review.mjs [outputDir] [tag]
import { chromium } from '@playwright/test';
import { mkdir } from 'node:fs/promises';
import path from 'node:path';

const BASE_URL = process.env.VISUAL_REVIEW_BASE_URL ?? 'http://localhost:5173';
const outDir = process.argv[2] ?? '/tmp/rich-visual-review';
const tag = process.argv[3] ?? 'run';

// Tab order on /rich-deliverable-fixture: richDeliverableVisualScenarios (11) + pulse + data-dashboard.
const TAB_INDEX = {
  'research-answer': 0,
  'incident-report': 1,
  'product-comparison': 2,
  'metrics-dashboard': 3,
  'implementation-plan': 4,
  'evidence-report': 5,
  'publication-report': 6,
  'newsletter-digest': 7,
  'freeform-notes': 8,
  'visual-system-reference': 9,
  'id-collision-report': 10,
  'daily-pulse': 11,
  'interactive-data-dashboard': 12,
};

const targets = [
  { route: '/rich-deliverable-pulse-fixture', name: 'pulse-embedded' },
  { route: '/rich-deliverable-prototype-fixture', name: 'prototype-390' },
  { route: '/rich-deliverable-fixture', tab: 'research-answer', name: 'research-answer' },
  { route: '/rich-deliverable-fixture', tab: 'product-comparison', name: 'product-comparison' },
  { route: '/rich-deliverable-fixture', tab: 'metrics-dashboard', name: 'metrics-dashboard' },
  { route: '/rich-deliverable-fixture', tab: 'incident-report', name: 'incident-report' },
  { route: '/rich-deliverable-fixture', tab: 'newsletter-digest', name: 'newsletter-digest' },
  { route: '/rich-deliverable-fixture', tab: 'freeform-notes', name: 'freeform-notes' },
  { route: '/rich-deliverable-fixture', tab: 'visual-system-reference', name: 'visual-system-reference' },
  { route: '/rich-deliverable-fixture', tab: 'daily-pulse', name: 'daily-pulse-standalone' },
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
        await page.goto(`${BASE_URL}${target.route}`, { waitUntil: 'networkidle', timeout: 15000 });
        if (target.tab) {
          const tabs = page.getByRole('tab');
          await tabs.nth(TAB_INDEX[target.tab]).click();
        }
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
