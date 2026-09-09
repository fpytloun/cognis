import { chromium } from '@playwright/test';
import { access, mkdir, writeFile } from 'node:fs/promises';
import path from 'node:path';
import { createContactSheet } from './lib/contact-sheet.mjs';
import { RICH_SCENARIO_WIDTHS, selectRichScenarios } from './lib/rich-scenario-files.mjs';
import { waitForRichFixture } from './lib/rich-screenshot.mjs';

const options = Object.fromEntries(process.argv.slice(2).map((argument) => {
  const [key, ...value] = argument.replace(/^--/, '').split('=');
  return [key, value.join('=') || 'true'];
}));
const group = options.group === 'all' || !options.group ? undefined : options.group;
const ids = options.ids ? options.ids.split(',').filter(Boolean) : undefined;
const themes = (options.themes ?? 'light').split(',');
const widths = (options.widths ?? '1280').split(',').map(Number);
const invalidWidths = widths.filter((width) => !RICH_SCENARIO_WIDTHS.includes(width));
if (invalidWidths.length) {
  throw new Error(`Unsupported widths: ${invalidWidths.join(', ')}. Use ${RICH_SCENARIO_WIDTHS.join(', ')}.`);
}
const outputDir = path.resolve(options.output ?? 'review-artifacts/rich-scenario-gallery/foundation');
const baseUrl = options['base-url'] ?? process.env.RICH_SCENARIO_BASE_URL ?? 'http://localhost:4173';
const scenarios = await selectRichScenarios({ group, ids });
await mkdir(path.join(outputDir, 'screenshots'), { recursive: true });
await mkdir(path.join(outputDir, 'contact-sheets'), { recursive: true });

const browser = await chromium.launch();
const captures = [];
for (const theme of themes) {
  for (const width of widths) {
    const entries = [];
    for (const scenario of scenarios) {
      const filename = `${scenario.id}--embedded--${theme}--${width}.png`;
      const outputPath = path.join(outputDir, 'screenshots', filename);
      if (options['contact-only'] === 'true') {
        await access(outputPath);
      } else {
        const page = await browser.newPage({ viewport: { width, height: 1400 }, colorScheme: theme });
        const query = new URLSearchParams({
          scenario: scenario.id, theme, width: String(width), surface: 'embedded',
        });
        await page.goto(`${baseUrl}/rich-deliverable-fixture?${query}`, { waitUntil: 'networkidle' });
        await waitForRichFixture(page);
        await page.screenshot({ path: outputPath, fullPage: false, animations: 'disabled' });
        await page.close();
      }
      entries.push({ path: outputPath, label: scenario.id });
      captures.push({ scenario_id: scenario.id, group: scenario.group, theme, width, surface: 'embedded', filename });
    }
    const sheetName = `${group ?? 'all'}--embedded--${theme}--${width}.png`;
    await createContactSheet(entries, path.join(outputDir, 'contact-sheets', sheetName));
  }
}
await browser.close();
await writeFile(path.join(outputDir, 'manifest.json'), `${JSON.stringify({
  schema_version: 1,
  group: group ?? 'all',
  captures,
}, null, 2)}\n`);
console.log(outputDir);
