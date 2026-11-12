import { expect, test } from '@playwright/test';
import { artifactId, deliverable, deliverableId, imageId, libraryItem } from './fixtures/resource-revisions/data';

test.use({ serviceWorkers: 'block' });

for (const width of [390, 1440]) {
  test.describe(`${width}px`, () => {
  test.use({ viewport: { width, height: 900 }, isMobile: width === 390, hasTouch: width === 390, timezoneId: 'Europe/Prague' });
  for (const theme of ['light', 'dark']) {
    for (const surface of ['library-deliverable', 'library-artifact', 'chat-deliverable', 'chat-inline', 'chat-artifact', 'chat-image']) {
      test(`${surface} has themed visible revision controls at ${width}px in ${theme}`, async ({ page }, info) => {
        const unmocked: string[] = [];
        const errors: string[] = [];
        page.on('pageerror', error => { errors.push(error.message); console.error(error.stack); });
        if (surface === 'chat-inline') await page.clock.setFixedTime(new Date('2026-11-04T10:59:00Z'));
        await page.route(url => url.pathname.startsWith('/api/'), async route => {
          const path = new URL(route.request().url()).pathname;
          const parts = path.split('/');
          const resourceId = parts[parts.length - 1];
          const parentId = parts[parts.length - 2];
          const json = (value: unknown) => route.fulfill({ json: value });
          const subjectId = 'subject_revision_controls';
          if (path === '/api/auth/me') {
            // The clock module is imported before auth bootstrap mounts the chat.
            if (surface === 'chat-inline') await page.clock.setFixedTime(new Date('2026-11-04T11:11:00Z'));
            return json({ email: 'revision-reader@cognis.test', name: 'Revision reader', role: 'user', is_active: true });
          }
          if (path === '/api/v1/feedback/sources/resolve' && route.request().method() === 'POST') {
            const request = route.request().postDataJSON();
            expect(request.source.kind).toBe('document');
            return json({
              subject_id: subjectId, source: request.source,
              source_token: `revision-fixture-${request.source.content_ref}`,
              context: request.context, content_digest: 'a'.repeat(64),
              format: request.source.content_ref.startsWith('dlv_') ? 'rich' : surface === 'chat-image' ? 'image' : 'plain',
              availability: 'available', permitted_locator_kinds: ['whole'],
              capabilities: { read: true, comment: true, moderate: true, dispatch: true, disclose: true },
              canonical_text: null,
            });
          }
          if (path === `/api/v1/feedback/subjects/${subjectId}/threads` && route.request().method() === 'GET') {
            return json({ items: [], next_cursor: null });
          }
          if (path.includes('/library/items/') && path.endsWith('/sharing')) {
            return json({ agents: [], project_id: null, owner_agent_id: null });
          }
          if (path.endsWith('/revisions')) {
            const isDeliverable = path.includes('/deliverables/');
            const id = isDeliverable ? deliverableId : path.includes(imageId) ? imageId : artifactId;
            const older = isDeliverable ? 'dlv_revision_older' : `${id}_older`;
            return json({
              lineage_id: id, latest_deliverable_id: id, latest_artifact_id: id, latest_revision: 2,
              revisions: [id, older].map((ref, index) => ({
                deliverable_id: ref, artifact_id: ref, revision: 2 - index, is_latest: index === 0,
                title: 'Release readiness report', format: 'markdown', filename: 'Readiness report.txt',
                mime_type: 'text/plain', size_bytes: 120, step_run_id: null,
                created_at: '2026-11-03T10:00:00Z',
                author_agent_id: index === 0 ? 'Riker' : 'krtek',
              })),
            });
          }
          if (path.includes('/library/items/')) {
            const kind = parentId === 'deliverable' ? 'deliverable' : 'artifact';
            return json(libraryItem(kind, resourceId));
          }
          if (path.includes('/library/deliverables/') && path.endsWith('/content')) return json(deliverable(parentId));
          if (/\/deliverables\/[^/]+$/.test(path)) return json(deliverable(resourceId));
          if (path.endsWith('/text-preview')) return json({ content: path.includes('older') ? 'Earlier artifact contents' : 'Current artifact contents', rich_payload: null, truncated: false });
          if (path.endsWith('/signed-url')) return json({ url: path.includes(imageId) ? '/image-older.svg' : '/report.txt' });
          if (path.endsWith('/versions')) return json([]);
          unmocked.push(path);
          return route.fulfill({ status: 404, json: { detail: 'Not provided by revision fixture' } });
        });
        await page.route('**/image*.svg', route => route.fulfill({ contentType: 'image/svg+xml', body: '<svg xmlns="http://www.w3.org/2000/svg" width="640" height="320"><rect width="640" height="320" fill="#527b71"/><text x="40" y="170" fill="white" font-size="32">Readiness diagram</text></svg>' }));
        const createdAt = surface === 'chat-inline' ? '&createdAt=2026-11-04T11%3A10%3A00Z' : '';
        await page.goto(`/e2e/fixtures/resource-revisions/index.html?surface=${surface}&theme=${theme}${createdAt}`);
        const prefix = surface.startsWith('library') ? 'library' : ['chat-deliverable', 'chat-inline'].includes(surface) ? 'deliverable' : surface === 'chat-image' ? 'image' : 'artifact';
        const toggle = page.getByTestId(`${prefix}-revision-toggle`);
        await expect(toggle).toBeVisible();
        await expect(toggle).toContainText('Revision 2 of 2');
        if (surface === 'chat-inline') {
          const footer = page.getByTestId('chat-document-footer');
          const identity = footer.getByTestId('deliverable-message-identity');
          await expect(identity).toContainText('LaForge');
          await expect(identity.locator('[title]')).toBeVisible();
          await expect(identity.locator('[title]')).toHaveText('12:10 · 1 minute ago');
          const identityBox = await identity.boundingBox();
          const controlsBox = await footer.getByRole('navigation', { name: 'Document actions' }).boundingBox();
          expect(identityBox!.x).toBeLessThan(controlsBox!.x);
          if (width === 1440) {
            expect(Math.abs(identityBox!.y + identityBox!.height / 2 - controlsBox!.y - controlsBox!.height / 2)).toBeLessThan(4);
          }
        }
        expect(await toggle.evaluate(node => node.closest('.chat-document-more, .compact-header-more') === null)).toBe(true);
        await toggle.click();
        const options = page.getByTestId(`${prefix}-revision-option`);
        await expect(options).toHaveCount(2);
        await expect(options.first()).toContainText('Latest');
        await expect(options.first()).toContainText('By Riker');
        await expect(options.last()).toContainText('By krtek');
        const menu = page.locator('.revision-panel:visible');
        await expect.poll(async () => {
          const box = await menu.boundingBox();
          return box !== null && box.x >= 0 && box.x + box.width <= width;
        }).toBe(true);
        const bounds = await menu.boundingBox();
        expect(bounds!.x).toBeGreaterThanOrEqual(0);
        expect(bounds!.x + bounds!.width).toBeLessThanOrEqual(width);
        const color = await menu.evaluate(node => getComputedStyle(node).backgroundColor);
        expect(color).not.toBe('rgba(0, 0, 0, 0)');
        await page.screenshot({ path: info.outputPath('revision-picker.png') });
        await toggle.press('Escape');
        await expect(toggle).toBeVisible();
        await toggle.click();
        await options.nth(1).click();
        await expect(page.getByTestId(`${prefix}-revision-toggle`)).toContainText('Revision 1 of 2');
        await expect(page.getByRole('alert')).toHaveCount(0);
        expect(errors).toEqual([]);
        expect(unmocked).toEqual([]);
        expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
      });
    }
  }
  });
}
