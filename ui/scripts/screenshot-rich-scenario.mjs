import { chromium } from '@playwright/test';
import { mkdir } from 'node:fs/promises';
import path from 'node:path';
import { RICH_SCENARIO_WIDTHS, selectRichScenarios } from './lib/rich-scenario-files.mjs';
import { defaultRichScreenshotPath } from './lib/rich-screenshot-paths.mjs';
import { waitForFullPageRichFixture } from './lib/rich-screenshot.mjs';

const id = process.argv[2] ?? 'research-answer';
const theme = process.argv[3] ?? 'light';
const width = Number(process.argv[4] ?? 1280);
if (!RICH_SCENARIO_WIDTHS.includes(width)) {
  throw new Error(`Unsupported width ${width}. Use ${RICH_SCENARIO_WIDTHS.join(', ')}.`);
}
const output = process.argv[5] ?? defaultRichScreenshotPath({ id, theme, width });
const baseUrl = process.env.RICH_SCENARIO_BASE_URL ?? 'http://localhost:4173';
await selectRichScenarios({ ids: [id] });
await mkdir(path.dirname(output), { recursive: true });
const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width, height: 1400 }, colorScheme: theme });
const query = new URLSearchParams({ scenario: id, theme, width: String(width), surface: 'embedded' });
await page.goto(`${baseUrl}/rich-deliverable-fixture?${query}`, { waitUntil: 'networkidle' });
await waitForFullPageRichFixture(page);
await page.screenshot({ path: output, fullPage: true, animations: 'disabled' });
await browser.close();
console.log(output);
