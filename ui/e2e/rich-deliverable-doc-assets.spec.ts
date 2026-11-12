import { expect, test } from '@playwright/test';
import { existsSync, mkdirSync, readFileSync, statSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';
import { parseEnv } from 'node:util';
import sharp from 'sharp';
import { SUPPORTED_RICH_BLOCK_TYPES } from '../src/lib/rich-deliverable';
import { richScenarioDocPreviews } from '../src/lib/rich-scenarios/docs-previews';
import { login } from './helpers';

const outputDir = join(process.cwd(), '../docs/assets/screenshots/rich-deliverables');
const updateAssets = process.env.UPDATE_RICH_DOC_ASSETS === '1';
const manifestPath = join(outputDir, 'manifest.json');
const maximumMismatchRatio = 0.005;
const showcaseCapture = process.env.SHOWCASE_SCREENSHOTS_ONLY === '1';

if (showcaseCapture) {
  test.use({ video: 'off', trace: 'off' });
  test.beforeEach(async ({ page }) => {
    const seedPath = process.env.SHOWCASE_MANIFEST;
    const privatePath = process.env.SHOWCASE_PRIVATE_ENV;
    if (!seedPath || !privatePath || (statSync(privatePath).mode & 0o777) !== 0o600) {
      throw new Error('Showcase manifest and private 0600 environment are required');
    }
    const seed = JSON.parse(readFileSync(seedPath, 'utf8'));
    const base = new URL(seed.base_url);
    if (!['http:', 'https:'].includes(base.protocol) ||
        !['localhost', '127.0.0.1', '[::1]'].includes(base.hostname) ||
        base.username || base.password || new URL(process.env.COGNIS_E2E_URL ?? '').origin !== base.origin) {
      throw new Error('Showcase base URL must match the local fixture');
    }
    const password = parseEnv(readFileSync(privatePath, 'utf8')).SHOWCASE_OWNER_PASSWORD;
    if (!password || !seed.users?.owner_email) throw new Error('Showcase owner credentials are required');
    await login(page, { email: seed.users.owner_email, password });
    await page.goto('/settings?tab=display');
    const main = page.getByRole('main');
    await main.getByRole('combobox', { name: /^Family\b/ }).selectOption('porcelain');
    await main.getByRole('combobox', { name: /^Appearance\b/ }).selectOption('light');
    await expect.poll(async () => {
      const response = await page.request.get('/api/v1/user-preferences');
      if (!response.ok()) return null;
      const preferences = await response.json();
      return [preferences.display?.theme_family, preferences.display?.theme];
    }).toEqual(['porcelain', 'light']);
  });
}

test.setTimeout(480_000);

async function waitForBlockReady(page: import('@playwright/test').Page, blockType: string) {
  const block = page.locator(`[data-rich-block-type="${blockType}"]`).first();
  await expect(block).toBeVisible();
  if (showcaseCapture) {
    const viewer = page.getByTestId('rich-deliverable').first();
    await expect(viewer).toHaveAttribute('data-theme-family', 'porcelain');
    await expect(viewer).toHaveAttribute('data-viewer-theme', 'light');
  }

  if (blockType === 'chart') await expect(block.locator('.rich-chart-canvas.chart-ready')).toBeVisible();
  if (blockType === 'mermaid') {
    await expect(block.locator('svg')).toBeVisible();
    const labelGeometry = await block.locator('foreignObject[width]').evaluateAll((labels) =>
      labels.map((label) => ({
        expected: Number(label.getAttribute('width')),
        actual: Number.parseFloat(getComputedStyle(label).width),
        transition: getComputedStyle(label).transitionDuration,
      }))
    );
    expect(labelGeometry.length).toBeGreaterThan(0);
    for (const label of labelGeometry) {
      expect(label.transition).toBe('0s');
      expect(label.actual).toBeCloseTo(label.expected, 1);
    }
  }
  const images = block.locator('img');
  for (let index = 0; index < await images.count(); index += 1) {
    await expect(images.nth(index)).toHaveJSProperty('complete', true);
    expect(await images.nth(index).evaluate((image: HTMLImageElement) => image.naturalWidth > 0)).toBe(true);
    await images.nth(index).evaluate((image: HTMLImageElement) => image.decode());
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

test('compact gallery toolbar matches its documentation image', async ({ page }) => {
  await page.setViewportSize({ width: 1230, height: 900 });
  await page.goto('/rich-deliverable-fixture?scenario=claude-viewer-parity&theme=light&width=1230&surface=embedded');
  const toolbar = page.getByTestId('rich-gallery-toolbar');
  await expect(toolbar).toBeVisible();
  await page.evaluate(() => document.fonts.ready);
  const image = await toolbar.screenshot({ animations: 'disabled' });
  const output = join(outputDir, 'gallery-toolbar.png');
  if (updateAssets) writeFileSync(output, image);
  else expect(await imageMismatchRatio(image, output)).toBeLessThanOrEqual(maximumMismatchRatio);
});

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
    if (showcaseCapture && preview.id === 'figure') {
      await page.setViewportSize({ width: 1280, height: 1100 });
    }
    const outputPath = join(outputDir, preview.filename);
    await page.goto(`/rich-deliverable-block-fixture?preview=${preview.id}`, {
      waitUntil: 'domcontentloaded',
    });
    const block = await waitForBlockReady(page, preview.block_type);
    if (preview.id === 'card-visual') {
      await expect(block).toContainText('The image appears behind the title and summary with a contrast overlay; it may be letterboxed.');
      await expect(block).not.toContainText(/full-bleed/i);
    }
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
