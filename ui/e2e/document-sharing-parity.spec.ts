import { expect, test, type Locator } from '@playwright/test';
import { deliverable, deliverableId, libraryItem } from './fixtures/resource-revisions/data';
import type { UserPreferences } from '../src/lib/types/api';

const fixturePath = process.env.COGNIS_TOC_FIXTURE_PATH ?? '/e2e/fixtures/resource-revisions/index.html';
const preferences: UserPreferences = {
  display: {
    theme: 'dark', theme_family: 'classic', language: 'en',
    dashboard_workspace_windows: false, pinned_agents_collapsed: false,
    pinned_agents_order_by_activity: false, conversation_sidebar_sections: true,
    conversation_sidebar_dense: false,
  },
  chat: {
    enter_to_send: true, show_thinking_blocks: false, group_tool_calls: true,
    keep_assistant_messages_separate: false, show_internal_tool_calls: false, auto_recap: true,
  },
  notifications: { include_content: false },
};

for (const family of ['classic', 'paper'] as const) {
for (const mode of ['dark', 'light'] as const) {
for (const width of [390, 1440]) {
  test.describe(`${family}-${mode}-${width}px`, () => {
    test.use({ viewport: { width, height: 900 }, isMobile: width === 390, hasTouch: width === 390 });
    test('tool-result and fullscreen sharing offer comment links for the same document', async ({ page, context }, info) => {
      await context.grantPermissions(['clipboard-read', 'clipboard-write']);
      const unexpected: string[] = [];
      const errors: string[] = [];
      const shares: unknown[] = [];
      page.on('pageerror', error => errors.push(error.message));
      await page.route(url => url.pathname.startsWith('/api/'), route => {
        const path = new URL(route.request().url()).pathname;
        const json = (value: unknown) => route.fulfill({ json: value });
        if (path === '/api/auth/me') return json({ email: 'owner@cognis.test', name: 'Owner', role: 'user', is_active: true });
        if (path === '/api/v1/user-preferences' && route.request().method() === 'GET') {
          return json({ ...preferences, display: { ...preferences.display, theme_family: family, theme: mode } });
        }
        if (path === '/api/v1/feedback/sources/resolve') return json({
          subject_id: 'sub_document_sharing', source_token: 'document-sharing-token',
          source: { kind: 'document', content_ref: deliverableId }, content_digest: 'document-digest',
          availability: 'available', permitted_locator_kinds: ['rich'], context: { kind: 'standalone' },
          lineage_id: deliverableId, revision: 2, latest_content_ref: deliverableId,
          capabilities: { read: true, comment: true, disclose: true, moderate: true, dispatch: true },
          title: 'Release readiness report', format: 'markdown', canonical_text: null,
        });
        if (path.endsWith('/threads')) return json({ items: [], next_cursor: null });
        if (path.endsWith('/public-shares') && route.request().method() === 'GET') return json({ items: [] });
        if (path.endsWith('/public-shares') && route.request().method() === 'POST') {
          shares.push(route.request().postDataJSON());
          return json({ share_token: 'synthetic-comment-link' });
        }
        if (path.endsWith('/revisions')) return json({ revisions: [] });
        if (path === '/api/v1/audio/jobs/source') return json({ jobs: [], supported: false });
        if (path.endsWith('/favorite')) return json({ favorite: false });
        if (path === `/api/v1/library/items/deliverable/${deliverableId}`) return json(libraryItem('deliverable'));
        if (path.startsWith('/api/v1/feedback/drafts/')) return route.fulfill({ status: 404, json: { detail: 'No saved draft' } });
        if (path === `/api/v1/deliverables/${deliverableId}`) return json({
          ...deliverable(), format: 'rich', rich_payload: {
            metadata: { toc: true },
            blocks: ['## Release overview', '### Verification', '#### Browser checks', '## Rollout']
              .map(content => ({ type: 'markdown', content: `${content}\n\n${'Release readiness evidence.\n\n'.repeat(10)}` })),
          },
        });
        unexpected.push(`${route.request().method()} ${path}`);
        return route.fulfill({ status: 404, json: { detail: `Not provided by sharing fixture: ${route.request().method()} ${path}` } });
      });
      await page.goto(`${fixturePath}?surface=tool-deliverable&theme=${mode}&family=${family}`);
      async function checkShare(root: Locator, capture: string) {
        if (width === 390) await root.getByTitle('More document actions').click();
        await root.getByRole('button', { name: 'Share link', exact: true }).click();
        const dialog = page.getByRole('dialog', { name: 'Share document', exact: true });
        await dialog.getByRole('tab', { name: 'Public links' }).click();
        await dialog.getByRole('combobox', { name: 'Link access and expiration' }).selectOption('7');
        await expect(dialog.getByRole('checkbox', { name: 'Allow comments' })).toBeEnabled();
        await dialog.getByRole('checkbox', { name: 'Allow comments' }).check();
        await expect(dialog.getByText('Existing public links', { exact: true })).toBeVisible();
        await dialog.getByRole('button', { name: 'Create public link', exact: true }).click();
        await expect(dialog.getByRole('textbox', { name: 'Public link URL' })).toHaveValue(/\/feedback#synthetic-comment-link$/);
        await expect(dialog.getByRole('button', { name: 'Create another link' })).toBeEnabled();
        await expect.poll(() => page.evaluate(() => navigator.clipboard.readText())).toMatch(/\/feedback#synthetic-comment-link$/);
        await expect(dialog.getByText('Select the link below and copy it manually.', { exact: true })).toHaveCount(0);
        await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
        await page.screenshot({ path: info.outputPath(capture) });
        await dialog.getByRole('button', { name: 'Done', exact: true }).click();
      }
      await checkShare(page.getByRole('main'), 'inline-share.png');
      await page.getByRole('button', { name: 'Open fullscreen', exact: true }).click();
      await expect(page.getByTestId('rich-deliverable-full-view')).toBeVisible();
      await checkShare(page.getByTestId('rich-deliverable-full-view'), 'fullscreen-share.png');
      expect(shares).toHaveLength(2);
      for (const share of shares) expect(share).toMatchObject({
        source: { kind: 'document', content_ref: deliverableId },
        expected_content_digest: 'document-digest', allow_comments: true, link_mode: 'view',
      });
      const full = page.getByTestId('rich-deliverable-full-view');
      await expect(full).toHaveAttribute('data-theme-family', family);
      if (width === 390) await full.getByRole('button', { name: 'Open table of contents' }).click();
      const nav = page.getByTestId('rich-deliverable-toc');
      const outline = width === 390 ? nav.getByRole('dialog', { name: 'Table of contents' }) : nav;
      await expect(page.locator('html')).toHaveAttribute('data-theme-family', family);
      await expect(page.locator('html')).toHaveAttribute('data-resolved-theme', mode);
      const overview = nav.getByRole('button', { name: 'Release overview', exact: true });
      const verification = nav.getByRole('button', { name: 'Verification', exact: true });
      const browser = nav.getByRole('button', { name: 'Browser checks', exact: true });
      const labelX = (row: Locator) => row.locator('span').last().boundingBox();
      expect((await labelX(verification))!.x).toBeGreaterThan((await labelX(overview))!.x);
      expect((await labelX(browser))!.x).toBeGreaterThan((await labelX(verification))!.x);
      // Indentation must not shrink the hit/hover/focus area of nested rows.
      expect((await browser.boundingBox())!.x).toBe((await overview.boundingBox())!.x);
      expect((await browser.boundingBox())!.width).toBe((await overview.boundingBox())!.width);
      for (const row of [overview, verification, browser]) {
        const bounds = (await row.boundingBox())!;
        const label = (await row.locator('span').last().boundingBox())!;
        const above = label.y - bounds.y;
        const below = bounds.y + bounds.height - label.y - label.height;
        expect(Math.abs(above - below)).toBeLessThanOrEqual(1);
      }
      await page.mouse.move(0, 0);
      await page.evaluate(() => document.fonts.ready);
      await page.screenshot({ path: info.outputPath('contents-idle.png'), animations: 'disabled' });
      await outline.screenshot({ path: info.outputPath('outline-idle.png'), animations: 'disabled' });
      await browser.focus();
      await expect(browser).toBeFocused();
      if (width === 390) expect((await browser.boundingBox())!.height).toBeGreaterThanOrEqual(44);
      else {
        await page.keyboard.press('Tab');
        await page.keyboard.press('Shift+Tab');
        await expect(browser).toBeFocused();
        expect(await browser.evaluate(el => getComputedStyle(el).outlineStyle)).not.toBe('none');
        // A single keyboard indicator replaces, not stacks on, the app button ring.
        expect(await browser.evaluate(el => getComputedStyle(el).boxShadow)).toBe('none');
        await page.screenshot({ path: info.outputPath('contents-keyboard.png'), animations: 'disabled' });
        await outline.screenshot({ path: info.outputPath('outline-keyboard.png'), animations: 'disabled' });
        await browser.click();
        await expect(full.getByRole('heading', { name: 'Browser checks', exact: true })).toBeInViewport();
        await expect(browser).toHaveAttribute('aria-current', 'location');
        expect(await browser.evaluate(el => el.matches(':focus-visible'))).toBe(false);
        const box = (await verification.boundingBox())!;
        await page.mouse.move(box.x + 2, box.y + box.height / 2);
        expect(await verification.evaluate(el => el.matches(':hover'))).toBe(true);
        expect(await verification.evaluate(el => getComputedStyle(el).backgroundColor)).not.toBe('rgba(0, 0, 0, 0)');
      }
      await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
      await page.evaluate(() => document.fonts.ready);
      await page.screenshot({ path: info.outputPath('hierarchical-contents.png'), animations: 'disabled' });
      await outline.screenshot({ path: info.outputPath('outline-hover.png'), animations: 'disabled' });
      if (width === 390) await browser.tap(); else await browser.press('Enter');
      await expect(full.getByRole('heading', { name: 'Browser checks', exact: true })).toBeInViewport();
      if (width === 390) await full.getByRole('button', { name: 'Open table of contents' }).click();
      await expect(nav.getByRole('button', { name: 'Browser checks', exact: true })).toHaveAttribute('aria-current', 'location');
      expect(unexpected).toEqual([]);
      expect(errors).toEqual([]);
    });
  });
}
}
}

for (const family of ['classic', 'paper'] as const) {
  test(`knowledge TOC ${family} keeps whole rows interactive`, async ({ page }, info) => {
    const unexpected: string[] = [];
    const errors: string[] = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.route(url => url.pathname.startsWith('/api/'), async route => {
      const path = new URL(route.request().url()).pathname;
      if (path === '/api/auth/me') {
        await route.fulfill({ json: { email: 'owner@cognis.test', name: 'Owner', role: 'user', is_active: true } });
      } else if (path === '/api/v1/user-preferences' && route.request().method() === 'GET') {
        await route.fulfill({ json: { ...preferences, display: { ...preferences.display, theme: 'light', theme_family: family } } });
      } else {
        unexpected.push(`${route.request().method()} ${path}`);
        await route.fulfill({ status: 404, json: { detail: 'Unexpected fixture request' } });
      }
    });
    await page.setViewportSize({ width: 1440, height: 900 });
    await page.goto(`${fixturePath}?surface=knowledge&theme=light&family=${family}`);
    const nav = page.getByRole('navigation', { name: 'Table of contents' });
    await expect(page.locator('html')).toHaveAttribute('data-theme-family', family);
    await expect(page.locator('html')).toHaveAttribute('data-resolved-theme', 'light');
    const overview = nav.getByRole('button', { name: 'Release overview', exact: true });
    const nested = nav.getByRole('button', { name: 'Browser checks with a longer label that wraps naturally', exact: true });
    await expect(nested).toBeVisible();
    const box = (await nested.boundingBox())!;
    expect(box.x).toBe((await overview.boundingBox())!.x);
    expect(box.width).toBe((await overview.boundingBox())!.width);
    expect(await nested.evaluate(el => el.scrollWidth <= el.clientWidth)).toBe(true);
    await page.mouse.move(box.x + 2, box.y + box.height / 2);
    expect(await nested.evaluate(el => el.matches(':hover'))).toBe(true);
    expect(await nested.evaluate(el => getComputedStyle(el).backgroundColor)).not.toBe('rgba(0, 0, 0, 0)');
    await nested.focus();
    await nested.press('Enter');
    await expect(nested).toHaveAttribute('aria-current', 'location');
    await expect(page.getByRole('heading', { name: 'Browser checks with a longer label that wraps naturally' })).toBeInViewport();
    await page.evaluate(() => document.fonts.ready);
    await page.screenshot({ path: info.outputPath('knowledge-contents.png'), animations: 'disabled' });
    expect(unexpected).toEqual([]);
    expect(errors).toEqual([]);
  });
}
