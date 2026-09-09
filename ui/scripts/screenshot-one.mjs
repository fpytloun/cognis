import { chromium } from '@playwright/test';

const scenario = process.argv[2] || 'publication-report';
const theme = process.argv[3] || 'light';
const outPath = process.argv[4] || `/tmp/rich-visual-review/${scenario}-${theme}.png`;

const browser = await chromium.launch();
const context = await browser.newContext({ viewport: { width: 1280, height: 1400 }, colorScheme: theme });
const page = await context.newPage();
const query = new URLSearchParams({ scenario, theme, width: '1280', surface: 'embedded' });
await page.goto(`http://localhost:4173/rich-deliverable-fixture?${query}`, { waitUntil: 'networkidle' });
await page.waitForTimeout(500);
await page.screenshot({ path: outPath, fullPage: true });
console.log(`Saved ${outPath}`);
await browser.close();
