import { expect, test } from '@playwright/test';
import { existsSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';
import sharp from 'sharp';
import { SUPPORTED_RICH_BLOCK_TYPES } from '../src/lib/rich-deliverable';
import { richScenarioDocPreviews } from '../src/lib/rich-scenarios/docs-previews';

const outputDir = join(process.cwd(), '../docs/assets/screenshots/rich-deliverables');
const updateAssets = process.env.UPDATE_RICH_DOC_ASSETS === '1';
const manifestPath = join(outputDir, 'manifest.json');
const maximumMismatchRatio = 0.005;

test.setTimeout(480_000);

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

async function imageMismatchRatio(actual: Buffer, expectedPath: string): Promise<number> {
  const expected = await sharp(expectedPath).ensureAlpha().raw().toBuffer({ resolveWithObject: true });
  const rendered = await sharp(actual).ensureAlpha().raw().toBuffer({ resolveWithObject: true });
  expect(rendered.info.width).toBe(expected.info.width);
  expect(rendered.info.height).toBe(expected.info.height);
  let mismatched = 0;
  const pixelCount = rendered.info.width * rendered.info.height;
  for (let offset = 0; offset < rendered.data.length; offset += 4) {
    if (
      Math.abs(rendered.data[offset] - expected.data[offset]) > 24
      || Math.abs(rendered.data[offset + 1] - expected.data[offset + 1]) > 24
      || Math.abs(rendered.data[offset + 2] - expected.data[offset + 2]) > 24
      || Math.abs(rendered.data[offset + 3] - expected.data[offset + 3]) > 24
    ) mismatched += 1;
  }
  return mismatched / pixelCount;
}

const blockTypeGroups = Array.from({ length: 4 }, (_, groupIndex) =>
  Array.from(SUPPORTED_RICH_BLOCK_TYPES).filter((_, index) => index % 4 === groupIndex)
);

for (const [groupIndex, blockTypes] of blockTypeGroups.entries()) {
  test(`renders supported rich deliverable block group ${groupIndex + 1}`, async ({ page }) => {
    await page.emulateMedia({ reducedMotion: 'reduce' });
    for (const blockType of blockTypes) {
      await page.goto(`/rich-deliverable-block-fixture?block=${blockType}`, {
        waitUntil: 'domcontentloaded',
      });
      const fixture = page.getByTestId('rich-deliverable-block-fixture');
      await expect(fixture).toHaveAttribute('data-block-type', blockType);
      await waitForBlockReady(page, blockType);
    }
  });
}

const documentationManifest = {
  schema_version: 1,
  previews: richScenarioDocPreviews.map((preview) => ({
    id: preview.id,
    scenario_id: preview.scenario_id,
    block_type: preview.block_type,
    occurrence: preview.occurrence ?? 0,
    variant: preview.variant ?? null,
    guide: preview.guide,
    filename: preview.filename,
  })),
};

test('documentation preview manifest matches the canonical registry', () => {
  if (updateAssets) {
    mkdirSync(outputDir, { recursive: true });
    writeFileSync(manifestPath, `${JSON.stringify(documentationManifest, null, 2)}\n`);
  } else {
    expect(JSON.parse(readFileSync(manifestPath, 'utf8'))).toEqual(documentationManifest);
  }
});

for (const preview of richScenarioDocPreviews) {
  test(`documentation preview ${preview.id} matches its committed image`, async ({ page }) => {
    await page.emulateMedia({ reducedMotion: 'reduce' });
    const outputPath = join(outputDir, preview.filename);
    await page.goto(`/rich-deliverable-block-fixture?preview=${preview.id}`, {
      waitUntil: 'domcontentloaded',
    });
    const block = await waitForBlockReady(page, preview.block_type);
    const screenshotTarget = preview.block_type === 'chart'
      ? page.getByTestId('rich-deliverable-block-fixture')
      : block;
    const screenshot = await screenshotTarget.screenshot({ animations: 'disabled' });

    if (updateAssets) {
      writeFileSync(outputPath, screenshot);
    } else {
      expect(existsSync(outputPath), `missing documentation screenshot: ${preview.filename}`).toBe(true);
      const mismatchRatio = await imageMismatchRatio(screenshot, outputPath);
      expect(
        mismatchRatio,
        `${preview.id} differs from ${preview.filename} by ${(mismatchRatio * 100).toFixed(2)}%`,
      ).toBeLessThanOrEqual(maximumMismatchRatio);
    }
  });
}
