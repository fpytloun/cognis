import { expect, test } from '@playwright/test';
import { existsSync, mkdirSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';
import { SUPPORTED_RICH_BLOCK_TYPES } from '../src/lib/rich-deliverable';

const outputDir = join(process.cwd(), '../docs/assets/screenshots/rich-deliverables');
const updateAssets = process.env.UPDATE_RICH_DOC_ASSETS === '1';
const chartVariants = ['line', 'bar', 'donut', 'stacked_bar'] as const;
// Additional named variants for block types where a single default
// screenshot does not cover a materially different visual treatment (see
// `?card=` handling in the block fixture route).
const cardVariants = [null, 'visual'] as const;

test.setTimeout(240_000);

async function waitForBlockReady(page: import('@playwright/test').Page, blockType: string) {
  const block = page.locator(`[data-rich-block-type="${blockType}"]`).first();
  await expect(block).toBeVisible();

  if (blockType === 'chart') await expect(block.locator('.rich-chart-canvas.chart-ready')).toBeVisible();
  if (blockType === 'mermaid') await expect(block.locator('svg')).toBeVisible();
  const images = block.locator('img');
  for (let index = 0; index < await images.count(); index += 1) {
    await expect(images.nth(index)).toHaveJSProperty('complete', true);
    expect(await images.nth(index).evaluate((image: HTMLImageElement) => image.naturalWidth > 0)).toBe(true);
  }
  await page.evaluate(async () => { await document.fonts?.ready; });
  let previous = await block.boundingBox();
  for (let attempt = 0; attempt < 4; attempt += 1) {
    await page.waitForTimeout(80);
    const current = await block.boundingBox();
    if (
      previous
      && current
      && Math.abs(previous.width - current.width) < 0.5
      && Math.abs(previous.height - current.height) < 0.5
    ) break;
    previous = current;
  }
  return block;
}

test('renders every supported rich deliverable block in the isolated fixture', async ({ page }) => {
  await page.emulateMedia({ reducedMotion: 'reduce' });
  for (const blockType of SUPPORTED_RICH_BLOCK_TYPES) {
    await page.goto(`/rich-deliverable-block-fixture?block=${blockType}`);
    const fixture = page.getByTestId('rich-deliverable-block-fixture');
    await expect(fixture).toHaveAttribute('data-block-type', blockType);
    await waitForBlockReady(page, blockType);
  }
});

test('documentation screenshots cover every supported block type', async ({ page }) => {
  await page.emulateMedia({ reducedMotion: 'reduce' });
  if (updateAssets) mkdirSync(outputDir, { recursive: true });

  for (const blockType of SUPPORTED_RICH_BLOCK_TYPES) {
    const variants = blockType === 'chart' ? chartVariants : blockType === 'card' ? cardVariants : [null];
    for (const variant of variants) {
      const filename = variant ? `${blockType}-${variant}.png` : `${blockType}.png`;
      const outputPath = join(outputDir, filename);
      const variantQuery = !variant ? '' : blockType === 'chart' ? `&chart=${variant}` : `&card=${variant}`;
      await page.goto(`/rich-deliverable-block-fixture?block=${blockType}${variantQuery}`);
      const block = await waitForBlockReady(page, blockType);
      const screenshotTarget = blockType === 'chart'
        ? page.getByTestId('rich-deliverable-block-fixture')
        : block;
      const screenshot = await screenshotTarget.screenshot({ animations: 'disabled' });

      if (updateAssets) {
        writeFileSync(outputPath, screenshot);
      } else {
        expect(existsSync(outputPath), `missing documentation screenshot: ${filename}`).toBe(true);
      }
      await expect(screenshotTarget).toHaveScreenshot(filename, { animations: 'disabled' });
    }
  }
});
