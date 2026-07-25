import { chromium } from '@playwright/test';

const TAB_INDEX = {
  'research-answer': 0,
  'incident-report': 1,
  'product-comparison': 2,
  'metrics-dashboard': 3,
  'implementation-plan': 4,
  'evidence-report': 5,
  'publication-report': 6,
  'visual-system-reference': 7,
  'id-collision-report': 8,
  'daily-pulse': 9,
  'interactive-data-dashboard': 10,
};

const scenario = process.argv[2] || 'publication-report';
const theme = process.argv[3] || 'light';
const outPath = process.argv[4] || `/tmp/rich-visual-review/${scenario}-${theme}.png`;

const browser = await chromium.launch();
const context = await browser.newContext({ viewport: { width: 1280, height: 1400 }, colorScheme: theme });
const page = await context.newPage();
await page.goto('http://localhost:4173/rich-deliverable-fixture', { waitUntil: 'networkidle' });
if (TAB_INDEX[scenario] !== undefined) {
  await page.getByRole('tab').nth(TAB_INDEX[scenario]).click();
}
await page.waitForTimeout(500);
await page.screenshot({ path: outPath, fullPage: true });
console.log(`Saved ${outPath}`);
await browser.close();
