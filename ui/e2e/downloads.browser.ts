/// <reference types="node" />
import { expect, test } from '@playwright/test';
import { readFile } from 'node:fs/promises';

for (const [format, filename, content] of [
  ['PDF', 'report.pdf', '%PDF-synthetic-download\n'],
  ['Single HTML', 'report.html', '<h1>Downloaded, never executed</h1>'],
  ['Markdown', 'report.md', '# Downloaded report\n'],
]) {
  test(`document ${format} saves original bytes without navigation`, async ({ page }) => {
    await page.goto('/');
    await page.locator('summary').click();
    const [download] = await Promise.all([
      page.waitForEvent('download'), page.getByRole('link', { name: format, exact: true }).click(),
    ]);
    expect(download.suggestedFilename()).toBe(filename);
    expect(await readFile((await download.path())!, 'utf8')).toBe(content);
    expect(page.url()).toBe('http://127.0.0.1:43128/');
  });
}

test('asynchronous attachment fetch saves binary bytes and keeps the viewer mounted', async ({ page }) => {
  await page.goto('/');
  const [download] = await Promise.all([
    page.waitForEvent('download'), page.getByRole('button', { name: 'Download attachment.bin' }).click(),
  ]);
  expect(download.suggestedFilename()).toBe('attachment.bin');
  expect(await readFile((await download.path())!)).toEqual(Buffer.from([0, 1, 127, 128, 255]));
  await expect(page.getByRole('button', { name: 'Download attachment.bin' })).toBeVisible();
});

for (const [kind, filename, content] of [
  ['workflow', 'workflow.yaml', 'name: Download acceptance\n'],
  ['recovery', 'cognis-recovery-codes.txt', 'synthetic-recovery-code\n'],
  ['skill', 'SKILL.md', '# Download acceptance\n'],
  ['recording', 'ws-recording.jsonl', '{"type":"synthetic-frame"}\n'],
]) {
  test(`shared ${kind} export preserves UTF-8 and filename`, async ({ page }) => {
    await page.goto('/');
    const [download] = await Promise.all([
      page.waitForEvent('download'), page.getByRole('button', { name: `Export ${kind}`, exact: true }).click(),
    ]);
    expect(download.suggestedFilename()).toBe(filename);
    expect(await readFile((await download.path())!, 'utf8')).toBe(content);
  });
}

test('shared package export preserves non-text bytes', async ({ page }) => {
  await page.goto('/');
  const [download] = await Promise.all([
    page.waitForEvent('download'), page.getByRole('button', { name: 'Export package' }).click(),
  ]);
  expect(download.suggestedFilename()).toBe('skill.zip');
  expect(await readFile((await download.path())!)).toEqual(Buffer.from([0, 1, 127, 128, 255]));
});
