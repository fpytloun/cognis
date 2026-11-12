import { expect, test, type Page } from '@playwright/test';
import { installLibraryFixture, libraryCatalog } from './library-fixture';
import type { LibraryItem } from '../src/lib/library';

async function authoringFixture(page: Page, mime = 'application/json', source?: string, richPayload?: unknown, filename?: string) {
  await installLibraryFixture(page);
  let current: LibraryItem = { ...libraryCatalog[7], id: 'art_authoring_config', title: mime === 'text/html' ? 'page.html' : mime === 'text/markdown' ? 'report.md' : 'config.json', format: mime, revision: 1 };
  if (filename) current.title = filename;
  let content = source ?? (mime === 'text/html' ? '<h1>Page preview</h1>' : '{"ready":true}');
  let explicit = false;
  let explicitWrite = false;
  await page.route('**/api/v1/library/**', async (route) => {
    const path = new URL(route.request().url()).pathname;
    const method = route.request().method();
    if (path.endsWith('/agents')) return route.fulfill({ json: [{ agent_id: 'agent_authoring', name: 'LaForge', explicit: false, inherited: false }, { agent_id: 'agent_mira', name: 'Mira', explicit: false, inherited: false }] });
    if (path.endsWith('/sharing')) {
      if (method === 'PUT') {
        const body = route.request().postDataJSON();
        explicit = body.agent_ids.includes('agent_mira');
        explicitWrite = body.write_agent_ids.includes('agent_mira');
      }
      return route.fulfill({ json: { agents: [
        { agent_id: 'agent_authoring', name: 'LaForge', explicit: false, inherited: true, inherited_permission: 'write', inherited_sources: ['owner'] },
        { agent_id: 'agent_mira', name: 'Mira', explicit, inherited: false, explicit_permission: explicit ? explicitWrite ? 'write' : 'read' : null },
      ], project_id: null, owner_agent_id: 'agent_authoring' } });
    }
    if (path.endsWith('/public-link')) return route.fulfill({ json: { url: 'https://cognis.test/shared/config', mode: route.request().postDataJSON().mode, expires_at: '2026-11-01T00:00:00Z' } });
    if (path.endsWith('/edit')) return route.fulfill({ json: { artifact_id: current.id, filename: current.title, mime_type: current.format, content } });
    if (path.endsWith('/revisions') && method === 'POST') {
      const body = route.request().postDataJSON();
      content = body.content;
      if (mime === 'text/markdown' && richPayload) richPayload = { blocks: [{ type: 'markdown', content }] };
      current = { ...current, id: 'art_authoring_revision', revision: 2, title: body.filename };
      return route.fulfill({ json: current });
    }
    if (path === '/api/v1/library/artifacts' && method === 'POST') {
      const body = route.request().postDataJSON(); content = body.content;
      current = { ...current, id: 'art_authored_new', title: body.filename, format: body.mime_type };
      return route.fulfill({ json: current });
    }
    if (path === '/api/v1/library/artifacts/upload') {
      expect(route.request().headers()['content-type']).toContain('multipart/form-data');
      return route.fulfill({ json: { ...current, id: 'att_uploaded_new', title: 'renamed.txt', format: 'text/plain' } });
    }
    if (path.includes('/items/artifact/art_') || path.includes('/items/artifact/att_')) return route.fulfill({ json: current });
    return route.fallback();
  });
  await page.route('**/api/v1/artifacts/**', (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path.endsWith('/signed-url')) return route.fulfill({ json: { url: '/authoring-preview', mode: 'view' } });
    if (path.endsWith('/text-preview')) {
      if (mime === 'text/html') return route.fulfill({ status: 415, json: { error: { message: 'HTML cannot be previewed as text' } } });
      return route.fulfill({ json: { content, truncated: false, rich_payload: richPayload } });
    }
    if (path.endsWith('/revisions')) return route.fulfill({ json: { lineage_id: current.id, latest_artifact_id: current.id, latest_revision: current.revision, revisions: [{ artifact_id: current.id, revision: current.revision, is_latest: true }] } });
    return route.fallback();
  });
  await page.route('**/authoring-preview', (route) => route.fulfill({ contentType: mime, body: content }));
}

test('Markdown renders on open, after cancel and after saving without entering preview mode', async ({ page }) => {
  const source = '# Artifact report\n\n**Ready for review**\n\n| Item | Status |\n| --- | --- |\n| Markdown | Rendered |';
  await authoringFixture(page, 'text/markdown', source, { blocks: [{ type: 'markdown', content: source }] });
  await page.goto('/library?lib_item=art_authoring_config&lib_item_kind=artifact');
  const modal = page.getByTestId('library-resource-modal');
  await expect(modal.getByRole('heading', { name: 'Artifact report' })).toBeVisible();
  await expect(modal.getByRole('table')).toContainText('Rendered');
  await expect(modal.getByRole('textbox', { name: 'Artifact content' })).toHaveCount(0);
  await page.screenshot({ path: test.info().outputPath('markdown-artifact-rendered.png'), fullPage: true });
  await modal.getByRole('button', { name: 'Edit', exact: true }).click();
  const editor = modal.getByRole('textbox', { name: 'Artifact content' });
  await expect(editor).toHaveText(source);
  await editor.fill('# Unsaved draft');
  await modal.getByRole('button', { name: 'Preview artifact' }).click();
  await expect(modal.getByRole('heading', { name: 'Unsaved draft' })).toBeVisible();
  await modal.getByRole('button', { name: 'Cancel', exact: true }).click();
  await expect(modal.getByRole('heading', { name: 'Artifact report' })).toBeVisible();
  await modal.getByRole('button', { name: 'Edit', exact: true }).click();
  await editor.fill('# Saved Markdown\n\nNew content');
  await modal.getByRole('button', { name: 'Save changes' }).click();
  await expect(page).toHaveURL(/lib_item=art_authoring_revision/);
  await expect(modal.getByRole('heading', { name: 'Saved Markdown' })).toBeVisible();
});

for (const width of [1280, 390]) {
  test(`CSV renders inert table cells immediately at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 844 });
    await authoringFixture(page, 'text/csv', 'name,value\nAlice,=1+1', {
      blocks: [{ type: 'table', columns: [{ key: 'name', label: 'name' }, { key: 'value', label: 'value' }],
        rows: [{ name: { type: 'code', value: 'Alice' }, value: { type: 'code', value: '=1+1' } }] }],
    }, 'data.csv');
    await page.goto('/library?lib_item=art_authoring_config&lib_item_kind=artifact');
    const modal = page.getByTestId('library-resource-modal');
    await expect(modal.getByRole('table')).toBeVisible();
    await expect(modal.locator('td code').first()).toHaveText('Alice');
    await expect(modal.getByText('=1+1', { exact: true })).toBeVisible();
    await page.screenshot({ path: test.info().outputPath(`csv-artifact-${width}.png`), fullPage: true });
  });
}

test('Mermaid uses the rich diagram renderer immediately in Library', async ({ page }) => {
  const source = 'graph TD\n  A[Input] --> B[Output]';
  await authoringFixture(page, 'text/vnd.mermaid', source, {
    blocks: [{ type: 'mermaid', source }],
  }, 'flow.mmd');
  await page.goto('/library?lib_item=art_authoring_config&lib_item_kind=artifact');
  const modal = page.getByTestId('library-resource-modal');
  await expect(modal.locator('.rich-mermaid svg')).toBeVisible({ timeout: 30_000 });
  await expect(modal.getByRole('button', { name: 'Edit', exact: true })).toBeVisible();
});

test('Library PDF uses the shared fitted multi-page viewer', async ({ page }) => {
  const objects = [
    '<< /Type /Catalog /Pages 2 0 R >>',
    '<< /Type /Pages /Kids [3 0 R 4 0 R] /Count 2 >>',
    '<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 300] /Resources << >> >>',
    '<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 300] /Resources << >> >>',
  ];
  let pdf = '%PDF-1.4\n';
  const offsets = [0];
  for (const [index, object] of objects.entries()) {
    offsets.push(pdf.length);
    pdf += `${index + 1} 0 obj\n${object}\nendobj\n`;
  }
  const xref = pdf.length;
  pdf += `xref\n0 5\n0000000000 65535 f \n${offsets.slice(1).map(offset => `${String(offset).padStart(10, '0')} 00000 n \n`).join('')}trailer\n<< /Size 5 /Root 1 0 R >>\nstartxref\n${xref}\n%%EOF`;
  await authoringFixture(page, 'application/pdf', pdf, undefined, 'report.pdf');
  await page.goto('/library?lib_item=art_authoring_config&lib_item_kind=artifact');
  const modal = page.getByTestId('library-resource-modal');
  await expect(modal.getByText('Page 1 / 2', { exact: true })).toBeVisible();
  const canvas = modal.getByLabel('PDF page 1');
  await expect.poll(() => canvas.evaluate(node => (node as HTMLCanvasElement).width)).toBeGreaterThan(0);
  await expect.poll(() => canvas.evaluate(node =>
    Math.abs(node.getBoundingClientRect().width - (node.parentElement!.clientWidth - 16)),
  )).toBeLessThan(2);
  await modal.getByRole('button', { name: 'Next', exact: true }).click();
  await expect(modal.getByText('Page 2 / 2', { exact: true })).toBeVisible();
  const contentBounds = await modal.getByRole('main', { name: 'Document preview' }).boundingBox();
  const controlsBounds = await modal.getByRole('button', { name: 'Fit width', exact: true }).boundingBox();
  expect(controlsBounds!.y + controlsBounds!.height).toBeLessThanOrEqual(contentBounds!.y + contentBounds!.height);
});

for (const media of ['audio', 'video'] as const) {
  test(`Library ${media} controls fit the constrained mobile preview`, async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 660 });
    await authoringFixture(page, `${media}/webm`, '', undefined, `recording.webm`);
    await page.goto('/library?lib_item=art_authoring_config&lib_item_kind=artifact');
    const modal = page.getByTestId('library-resource-modal');
    const player = modal.locator(media);
    await expect(player).toBeVisible();
    await expect(player).toHaveAttribute('controls');
    const content = await modal.getByRole('main', { name: 'Document preview' }).boundingBox();
    const bounds = await player.boundingBox();
    expect(bounds!.x).toBeGreaterThanOrEqual(content!.x);
    expect(bounds!.y).toBeGreaterThanOrEqual(content!.y);
    expect(bounds!.x + bounds!.width).toBeLessThanOrEqual(content!.x + content!.width);
    expect(bounds!.y + bounds!.height).toBeLessThanOrEqual(content!.y + content!.height);
  });
}

test('edit JSON in the same pane, validate, preview, cancel and save a new revision', async ({ page }) => {
  await authoringFixture(page);
  await page.goto('/library?lib_item=art_authoring_config&lib_item_kind=artifact&lib_kind=artifact');
  const modal = page.getByTestId('library-resource-modal');
  await expect(modal.locator('.json-key').first()).toBeVisible({ timeout: 30_000 });
  await modal.getByRole('button', { name: 'Edit', exact: true }).click();
  const editor = modal.getByRole('textbox', { name: 'Artifact content' });
  await editor.fill('{"unfinished":');
  await expect(modal.getByRole('button', { name: 'Save changes' })).toBeDisabled();
  await editor.fill('{"ready":false,"items":[1,2]}');
  await modal.getByRole('button', { name: 'Preview artifact' }).click();
  await expect(editor).toBeHidden();
  await modal.getByRole('button', { name: 'Preview artifact' }).click();
  await expect(editor).toHaveText('{"ready":false,"items":[1,2]}');
  await page.screenshot({ path: test.info().outputPath('artifact-edit-json.png'), fullPage: true });
  await modal.getByRole('button', { name: 'Cancel', exact: true }).click();
  await expect(modal.getByText(/"ready": true/)).toBeVisible();
  await modal.getByRole('button', { name: 'Edit', exact: true }).click();
  await editor.fill('{"ready":false}');
  await modal.getByRole('button', { name: 'Save changes' }).click();
  await expect(page).toHaveURL(/lib_item=art_authoring_revision/);
  await expect(modal.getByRole('button', { name: 'Edit', exact: true })).toBeVisible();
  await modal.getByRole('button', { name: 'Manage agent access' }).click();
  const access = page.getByRole('dialog', { name: 'Share with agents', exact: true });
  await expect(access.getByRole('button', { name: 'Share with LaForge' })).toBeDisabled();
  await access.getByRole('button', { name: 'Share with Mira' }).click();
  await access.getByRole('combobox', { name: 'Explicit permission for Mira' }).selectOption('write');
  await page.screenshot({ path: test.info().outputPath('agent-avatar-selection.png'), fullPage: true });
  await access.getByRole('button', { name: 'Save agent access' }).click();
  await expect(access).toHaveCount(0);
  await expect(modal.getByLabel('Mira · Explicit: read + write', { exact: true })).toBeVisible();
  await modal.getByRole('button', { name: 'Manage agent access' }).click();
  await expect(access.getByRole('combobox', { name: 'Explicit permission for Mira' })).toHaveValue('write');
  await access.getByRole('combobox', { name: 'Explicit permission for Mira' }).selectOption('read');
  await access.getByRole('button', { name: 'Save agent access' }).click();
  await expect(modal.getByLabel('Mira · Explicit: read', { exact: true })).toBeVisible();
  const share = modal.getByRole('button', { name: 'Share link' });
  const before = await share.evaluate((node) => getComputedStyle(node).color);
  await share.hover();
  await expect.poll(() => share.evaluate((node) => getComputedStyle(node).color)).not.toBe(before);
  await share.click();
  const publicDialog = page.getByRole('dialog', { name: 'Share link', exact: true });
  await expect(publicDialog.getByRole('combobox', { name: 'Link access and expiration' })).toHaveValue('private');
  await publicDialog.getByRole('combobox', { name: 'Link access and expiration' }).selectOption('7');
  await expect(publicDialog.getByRole('combobox', { name: 'Link behavior' })).toHaveValue('view');
  await expect(publicDialog.getByText('Agent access', { exact: true })).toHaveCount(0);
  await publicDialog.getByRole('button', { name: 'Create public link' }).click();
  await expect(publicDialog.getByRole('textbox', { name: 'Public link URL', exact: true })).toHaveValue('https://cognis.test/shared/config');
  await page.screenshot({ path: test.info().outputPath('artifact-sharing.png'), fullPage: true });
});

test.describe('phone document permissions', () => {
  test.use({ viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true });

  test('write access persists and can be downgraded to read', async ({ page }, info) => {
    await authoringFixture(page);
    await page.goto('/library?lib_item=art_authoring_config&lib_item_kind=artifact');
    const modal = page.getByTestId('library-resource-modal');
    await modal.getByRole('button', { name: 'Show details', exact: true }).tap();
    const manage = modal.getByRole('button', { name: 'Manage agent access' });
    await expect(manage).toBeEnabled();
    await manage.tap();
    const access = page.getByRole('dialog', { name: 'Share with agents', exact: true });
    await access.getByRole('button', { name: 'Share with Mira' }).tap();
    const permission = access.getByRole('combobox', { name: 'Explicit permission for Mira' });
    const permissionBounds = await permission.boundingBox();
    expect(permissionBounds?.height).toBeGreaterThanOrEqual(44);
    await permission.selectOption('write');
    await expect(permission).toHaveValue('write');
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(390);
    await page.screenshot({ path: info.outputPath('phone-write-permission.png') });
    await access.getByRole('button', { name: 'Save agent access' }).tap();
    await expect(modal.getByLabel('Mira · Explicit: read + write', { exact: true })).toBeVisible();
    await manage.tap();
    await expect(permission).toHaveValue('write');
    await permission.selectOption('read');
    await access.getByRole('button', { name: 'Save agent access' }).tap();
    await expect(modal.getByLabel('Mira · Explicit: read', { exact: true })).toBeVisible();
  });
});

test('HTML opens successfully and editing preview remains sandboxed', async ({ page }) => {
  await authoringFixture(page, 'text/html');
  await page.goto('/library?lib_item=art_authoring_config&lib_item_kind=artifact');
  const modal = page.getByRole('dialog');
  await expect(modal.locator('iframe')).toBeVisible({ timeout: 30_000 });
  await modal.getByRole('button', { name: 'Edit', exact: true }).click();
  await modal.getByRole('textbox', { name: 'Artifact content' }).fill('<h1>Updated preview</h1>');
  await modal.getByRole('button', { name: 'Preview artifact' }).click();
  await expect(modal.getByTitle('Artifact preview')).toHaveAttribute('sandbox', 'allow-scripts');
  await expect(page.frameLocator('iframe[title="Artifact preview"]').getByRole('heading', { name: 'Updated preview' })).toBeVisible();
});

test('create a named text artifact and protect draft against navigation', async ({ page }) => {
  await authoringFixture(page);
  await page.goto('/library');
  await page.getByRole('combobox', { name: 'View', exact: true }).selectOption('conversation');
  await page.getByRole('button', { name: 'New artifact' }).click();
  const modal = page.getByRole('dialog');
  await modal.getByRole('textbox', { name: 'Artifact name' }).fill('notes.md');
  await modal.getByRole('combobox', { name: 'Artifact format' }).selectOption('text/markdown');
  await modal.getByRole('textbox', { name: 'New artifact content' }).fill('# Human-authored notes\n\nSaved as a durable artifact.');
  await page.screenshot({ path: test.info().outputPath('artifact-create.png'), fullPage: true });
  page.once('dialog', (dialog) => dialog.dismiss());
  await page.goBack();
  await expect(modal).toBeVisible();
  await modal.getByRole('button', { name: 'Select agents' }).click();
  const picker = page.getByRole('dialog', { name: 'Share with agents', exact: true });
  await picker.getByRole('button', { name: 'Share with LaForge' }).click();
  await picker.getByRole('button', { name: 'Done', exact: true }).click();
  await modal.getByRole('button', { name: 'Create artifact', exact: true }).click();
  await expect(page).toHaveURL(/lib_item=art_authored_new/);
  await expect(page.getByTestId('library-resource-modal')).toBeVisible();
});

test('upload a file with editable auto-filled name on mobile', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await authoringFixture(page);
  await page.goto('/library');
  await page.getByRole('button', { name: 'New artifact' }).click();
  const modal = page.getByRole('dialog');
  await modal.getByRole('tab', { name: 'Upload file', exact: true }).click();
  await modal.getByLabel('Upload file', { exact: true }).setInputFiles({ name: 'brief.txt', mimeType: 'text/plain', buffer: Buffer.from('User uploaded text') });
  await expect(modal.getByRole('textbox', { name: 'Artifact name' })).toHaveValue('brief.txt');
  await modal.getByRole('textbox', { name: 'Artifact name' }).fill('renamed.txt');
  await page.screenshot({ path: test.info().outputPath('artifact-mobile-upload.png'), fullPage: true });
  await modal.getByRole('button', { name: 'Create artifact', exact: true }).click();
  await expect(page).toHaveURL(/lib_item=att_uploaded_new/);
});

test('access management waits for grants and never starts from an empty selection', async ({ page }) => {
  await authoringFixture(page);
  let release!: () => void;
  const ready = new Promise<void>(resolve => { release = resolve; });
  await page.route('**/api/v1/library/items/artifact/*/sharing', async route => {
    await ready;
    return route.fulfill({ json: {
      owner_agent_id: 'agent_authoring', project_id: null,
      agents: [
        { agent_id: 'agent_authoring', name: 'LaForge', explicit: false, inherited: true },
        { agent_id: 'agent_mira', name: 'Mira', explicit: true, inherited: false },
      ],
    } });
  });
  await page.goto('/library?lib_item=art_authoring_config&lib_item_kind=artifact');
  const manage = page.getByRole('button', { name: 'Manage agent access' });
  await expect(manage).toBeDisabled();
  release();
  await expect(manage).toBeEnabled();
  await manage.click();
  const access = page.getByRole('dialog', { name: 'Share with agents', exact: true });
  await expect(access.getByRole('button', { name: 'Share with Mira' })).toHaveAttribute('aria-pressed', 'true');
  await expect(access.getByRole('button', { name: 'Save agent access' })).toBeDisabled();
  await page.keyboard.press('Escape');
  await expect(manage).toBeFocused();
  await expect(page.getByTestId('library-resource-modal')).toBeVisible();
});

test('mobile reader separates metadata from public sharing and restores focus', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await authoringFixture(page);
  await page.goto('/library?lib_item=art_authoring_config&lib_item_kind=artifact');
  const modal = page.getByTestId('library-resource-modal');
  await expect(modal.locator('.json-key').first()).toBeVisible({ timeout: 30_000 });
  await expect(modal.getByRole('heading', { name: 'Details', exact: true })).toHaveCount(0);
  await page.screenshot({ path: test.info().outputPath('artifact-mobile-reader.png'), fullPage: true });
  await modal.getByRole('button', { name: 'Show details' }).click();
  await expect(modal.getByRole('heading', { name: 'Details', exact: true })).toBeVisible();
  await page.screenshot({ path: test.info().outputPath('artifact-mobile-details.png'), fullPage: true });
  const share = modal.getByRole('button', { name: 'Share link' });
  await share.click();
  const publicDialog = page.getByRole('dialog', { name: 'Share link', exact: true });
  await expect(publicDialog).toBeVisible();
  await expect(modal.getByRole('button', { name: 'Copy internal link' })).toHaveCount(0);
  await expect(publicDialog.getByRole('combobox', { name: 'Link access and expiration' })).toHaveValue('private');
  await expect(publicDialog.getByRole('button', { name: 'Copy private link', exact: true })).toBeVisible();
  await expect(publicDialog.getByRole('button', { name: 'Create public link' })).toHaveCount(0);
  await expect(publicDialog.getByRole('button', { name: 'Manage agent access' })).toHaveCount(0);
  const bounds = await publicDialog.boundingBox();
  expect(bounds?.x).toBeGreaterThanOrEqual(0);
  expect((bounds?.x ?? 0) + (bounds?.width ?? 0)).toBeLessThanOrEqual(390);
  await page.screenshot({ path: test.info().outputPath('artifact-mobile-public-link.png'), fullPage: true });
  await page.keyboard.press('Escape');
  await expect(publicDialog).toHaveCount(0);
  await expect(share).toBeFocused();
  await expect(modal).toBeVisible();
});

test('revision history failure does not hide readable artifact content', async ({ page }) => {
  await authoringFixture(page);
  await page.route('**/api/v1/artifacts/*/revisions', route => route.fulfill({ status: 503, json: { detail: 'History unavailable' } }));
  await page.goto('/library?lib_item=art_authoring_config&lib_item_kind=artifact');
  await expect(page.getByTestId('library-resource-modal').locator('.json-key').first()).toBeVisible({ timeout: 30_000 });
  await expect(page.getByRole('alert')).toHaveText('Revision history is unavailable. You can still read this document.');
});

test('chat full-view public dialog stacks above the viewer and closes independently', async ({ page }) => {
  await page.goto('/rich-deliverable-fixture?scenario=research-answer&surface=embedded&sharing=1&capture=viewer');
  await page.getByRole('button', { name: 'Open full view' }).click();
  const viewer = page.getByTestId('rich-deliverable-full-view');
  await expect(viewer).toBeVisible();
  await expect(viewer.getByRole('button', { name: 'Copy internal link' })).toHaveCount(0);
  const share = viewer.getByRole('button', { name: 'Share link' });
  await share.click();
  const dialog = page.getByRole('dialog', { name: 'Share link', exact: true });
  await expect(dialog).toBeVisible();
  await expect(dialog.getByRole('combobox', { name: 'Link access and expiration' })).toHaveValue('private');
  expect(await dialog.evaluate(node => {
    const rect = node.getBoundingClientRect();
    return node.contains(document.elementFromPoint(rect.x + rect.width / 2, rect.y + rect.height / 2));
  })).toBe(true);
  await page.keyboard.press('Tab');
  expect(await dialog.evaluate(node => node.contains(document.activeElement))).toBe(true);
  await page.keyboard.press('Escape');
  await expect(dialog).toHaveCount(0);
  await expect(viewer).toBeVisible();
  await expect(share).toBeFocused();
  await page.keyboard.press('Escape');
  await expect(viewer).toHaveCount(0);
});
