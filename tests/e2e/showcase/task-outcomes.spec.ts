import { expect, test } from '../../../ui/node_modules/@playwright/test';
import { mkdir, readFile, writeFile } from 'node:fs/promises';
import { parseEnv } from 'node:util';
import { resolve } from 'node:path';

test('task outcome chat desktop and phone', async ({ browser }) => {
  const seed = JSON.parse(await readFile(process.env.SHOWCASE_MANIFEST!, 'utf8'));
  const privateEnv = parseEnv(await readFile(process.env.SHOWCASE_PRIVATE_ENV!, 'utf8'));
  const fixture = seed.workspace.task_outcomes;
  const revision = seed.source_revision.slice(0, 12);
  expect(fixture?.conversation_id).toBeTruthy();
  const output = resolve(process.env.SHOWCASE_OUTPUT!, 'task-outcomes');
  await mkdir(output, { recursive: true });
  for (const [name, viewport] of [
    ['desktop', { width: 1280, height: 800 }],
    ['phone', { width: 390, height: 848 }],
  ] as const) {
    const context = await browser.newContext({
      viewport, deviceScaleFactor: 2, isMobile: name === 'phone', hasTouch: name === 'phone',
      reducedMotion: 'reduce', serviceWorkers: 'block',
    });
    const page = await context.newPage();
    const mutations: string[] = [];
    try {
      await page.goto(seed.base_url);
      await page.getByLabel('Email', { exact: true }).fill(seed.users.owner_email);
      await page.getByLabel('Password', { exact: true }).fill(privateEnv.SHOWCASE_OWNER_PASSWORD!);
      await page.getByRole('button', { name: 'Sign in', exact: true }).click();
      await expect(page.getByTestId('control-center')).toBeVisible();
      page.on('request', request => {
        if (!['GET', 'HEAD', 'OPTIONS'].includes(request.method())) {
          mutations.push(`${request.method()} ${new URL(request.url()).pathname}`);
        }
      });
      await page.goto(new URL(`/chat/${fixture.conversation_id}`, seed.base_url).href);
      const footer = page.getByTestId('task-outcome-footer');
      await expect(footer).toContainText('Daily launch brief · Attempt 2');
      await expect(page.getByText('Stock and pricing are verified.', { exact: false })).toBeVisible();
      const card = page.getByTestId('task-outcome-card');
      await expect(card.getByText('Partial output — task failed')).toBeVisible();
      await expect(page.getByText(/Loading deliverable/)).toHaveCount(0);
      await page.evaluate(async () => { await document.fonts.ready; });
      await footer.scrollIntoViewIfNeeded();
      await page.screenshot({ path: `${output}/${revision}-${name}-success.png` });
      await footer.getByRole('button').click();
      await expect(page.getByRole('dialog')).toContainText('Daily launch brief');
      await expect(page.getByTestId('dashboard-entity-modal-external-link')).toBeVisible();
      await page.keyboard.press('Escape');
      await expect(page.getByRole('dialog')).toBeHidden();
      await expect(card).toContainText('The supplier feed did not respond.');
      await expect(card.getByText('Partial output — task failed')).toBeVisible();
      await card.scrollIntoViewIfNeeded();
      await page.screenshot({ path: `${output}/${revision}-${name}-failed-collapsed.png` });
      await card.getByRole('button', { name: 'Show steps', exact: true }).click();
      await expect(card.getByRole('list')).toContainText('Collect sources');
      await expect(card.getByRole('list')).toContainText('Verify availability');
      const geometry = await page.evaluate(() => {
        const card = document.querySelector('[data-testid="task-outcome-card"]')!;
        const input = [...document.querySelectorAll('textarea, [contenteditable="true"]')]
          .find(node => node.getClientRects().length > 0);
        if (!input) throw new Error('The visible chat composer is missing');
        return { cardBottom: card.getBoundingClientRect().bottom, inputTop: input.getBoundingClientRect().top };
      });
      expect(geometry.cardBottom).toBeLessThanOrEqual(geometry.inputTop + 1);
      console.log(JSON.stringify({ viewport: name, geometry }));
      await page.screenshot({ path: `${output}/${revision}-${name}-failed-expanded.png` });
      await card.getByRole('button', { name: 'Open task', exact: true }).click();
      const dialog = page.getByRole('dialog');
      await expect(dialog).toContainText('Refresh supplier availability');
      await expect(dialog.getByTestId('dashboard-entity-modal-external-link')).toBeVisible();
      await expect(dialog.getByText('Loading task…', { exact: true })).toBeHidden();
      await expect(dialog.getByText('Partial output — task failed')).toBeVisible();
      await expect(dialog.getByText(/Loading deliverable/)).toHaveCount(0);
      const bounds = (await dialog.boundingBox())!;
      expect(bounds.x).toBeGreaterThanOrEqual(-1);
      expect(bounds.y).toBeGreaterThanOrEqual(-1);
      expect(bounds.x + bounds.width).toBeLessThanOrEqual(viewport.width + 1);
      expect(bounds.y + bounds.height).toBeLessThanOrEqual(viewport.height + 1);
      await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
      await page.screenshot({ path: `${output}/${revision}-${name}-task-modal.png` });
      await page.keyboard.press('Escape');
      await expect(dialog).toBeHidden();
      const handling = page.getByTestId('task-handling-row');
      await expect(handling.getByRole('button')).toHaveText('Handling task: Check courier handoff (failed)');
      await expect(card.filter({ hasText: 'Check courier handoff' })).toHaveCount(0);
      await handling.scrollIntoViewIfNeeded();
      await page.screenshot({ path: `${output}/${revision}-${name}-handling.png` });
      await handling.getByRole('button').click();
      await expect(page.getByRole('dialog')).toContainText('Check courier handoff');
      await page.keyboard.press('Escape');
      await expect(page.getByRole('dialog')).toBeHidden();
      const overflow = await page.evaluate(() => document.documentElement.scrollWidth > innerWidth);
      expect(overflow).toBe(false);
      console.log(JSON.stringify({ viewport: name, mutations }));
    } catch (error) {
      await page.screenshot({ path: `${output}/${revision}-${name}-failure.png` });
      await writeFile(`${output}/${revision}-${name}-failure.html`, await page.content());
      throw error;
    } finally {
      await context.close();
    }
  }
});
