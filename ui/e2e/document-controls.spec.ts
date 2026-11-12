import { expect, test, type Page } from '@playwright/test';
import { installLibraryFixture, installLibraryRoutes, libraryCatalog } from './library-fixture';

test.use({ viewport: { width: 390, height: 844 }, hasTouch: true });

for (const host of ['chat', 'fullscreen', 'work', 'library-deliverable', 'library-artifact', 'library-read-only'] as const) {
  test(`${host} uses the private-first share picker without minting a public token`, async ({ page }) => {
    await installLibraryFixture(page);
    await page.addInitScript(() => Object.defineProperty(navigator, 'clipboard', { value: {
      writeText: async (value: string) => { localStorage.setItem('private-share-copy', value); },
    } }));
    await installLibraryRoutes(page, [
      { ...libraryCatalog[0], id: 'chat-flow-review', lineage_id: 'chat-flow-review' },
      libraryCatalog[8],
      libraryCatalog[6],
    ]);
    await page.route('**/api/v1/artifacts/*/revisions', route => route.fulfill({ json: { revisions: [] } }));
    await page.route('**/api/v1/artifacts/*/text-preview', route => route.fulfill({ json: {
      artifact_id: libraryCatalog[8].id, filename: 'requirements.txt', mime_type: 'text/plain',
      size_bytes: 12, content: 'Requirements', truncated: false, preview_kind: 'text', rich_payload: null,
    } }));
    let publicRequests = 0;
    await page.route(/\/api\/v1\/.*\/(?:public-link|share-link)$/, route => {
      publicRequests++;
      return route.fulfill({ json: { url: `${new URL(page.url()).origin}/shared/report` } });
    });
    const artifact = host === 'library-artifact';
    const id = artifact ? libraryCatalog[8].id : host === 'library-read-only' ? libraryCatalog[6].id : 'chat-flow-review';
    const path = `/library/${artifact ? 'artifact' : 'deliverable'}/${id}`;
    await page.goto(host.startsWith('library') ? path : `/chat-deliverable-fixture?favorites${host === 'work' ? '&work' : ''}`);
    if (host === 'fullscreen') await page.getByRole('button', { name: 'Open fullscreen' }).tap();
    if (host === 'work') {
      await page.getByTitle('More document actions').tap();
      await page.getByRole('button', { name: 'Expand document', exact: true }).tap();
    }
    await page.getByRole('button', { name: 'Share link', exact: true }).tap();
    const dialog = page.getByRole('dialog', { name: 'Share link', exact: true });
    const selector = dialog.getByRole('combobox', { name: 'Link access and expiration' });
    await expect(selector).toHaveValue('private');
    const choices = [
      'Private · internal access only', '1 day', '7 days', '30 days', '90 days', 'Custom date and time',
    ];
    await expect(selector.locator('option')).toHaveText(host === 'library-read-only' ? choices.slice(0, 1) : choices);
    await dialog.getByRole('button', { name: 'Copy private link', exact: true }).tap();
    await expect(dialog.getByRole('textbox', { name: 'Private link URL' })).toHaveValue(`${new URL(page.url()).origin}${path}`);
    await expect.poll(() => page.evaluate(() => localStorage.getItem('private-share-copy'))).toBe(`${new URL(page.url()).origin}${path}`);
    expect(publicRequests).toBe(0);
    if (host === 'library-read-only') {
      await expect(dialog.getByRole('button', { name: 'Create public link', exact: true })).toHaveCount(0);
      return;
    }
    await selector.selectOption('7');
    expect(publicRequests).toBe(0);
    await dialog.getByRole('button', { name: 'Create public link', exact: true }).tap();
    await expect.poll(() => publicRequests).toBe(1);
    await expect(dialog.getByRole('textbox', { name: 'Public link URL' })).toBeVisible();
  });
}

test('chat overflow actions execute on touch and stay inside the viewport', async ({ page }) => {
  await installLibraryFixture(page);
  await page.addInitScript(() => Object.defineProperty(navigator, 'clipboard', { value: {
    writeText: async (value: string) => { localStorage.setItem('document-controls-copy', value); },
  } }));
  await installLibraryRoutes(page, [{ ...libraryCatalog[0], id: 'chat-flow-review', lineage_id: 'chat-flow-review' }]);
  await page.goto('/chat-deliverable-fixture?favorites');
  const footer = page.getByTestId('chat-document-footer');
  await footer.getByTitle('More document actions').tap();
  const favorite = footer.getByRole('button', { name: 'Add to favorites', exact: true });
  await expect(favorite).toBeEnabled();
  await favorite.tap();
  await expect(footer.getByRole('button', { name: 'Remove from favorites' })).toHaveAttribute('aria-pressed', 'true');
  // Mobile browsers may blur the summary without focusing a tapped button.
  await footer.locator('summary[aria-label="More document actions"]').evaluate(summary => {
    summary.dispatchEvent(new FocusEvent('focusout', { bubbles: true, relatedTarget: document.body }));
  });
  await footer.getByRole('button', { name: 'Copy document' }).tap();
  await expect.poll(() => page.evaluate(() => localStorage.getItem('document-controls-copy'))).toBe('The release is ready for staging. Verify recovery before production.');
  await footer.getByTitle('More document actions').tap();
  await footer.getByRole('link', { name: 'Open in Library ↗' }).tap();
  await expect(page).toHaveURL(/\/library\/deliverable\/chat-flow-review/);
});

const parityDocument = {
  deliverable_id: 'chat-flow-review', title: 'Reader parity', format: 'rich',
  content: 'A shared document across every host.', revision: 1, latest_revision: 1,
  is_latest: true, lineage_id: 'chat-flow-review', latest_deliverable_id: 'chat-flow-review',
  rich_payload: { metadata: { toc: { enabled: true } }, blocks: [
    { type: 'hero', title: 'Reader parity', subtitle: 'One renderer, every host.' },
    { type: 'markdown', content: '## First section\n\nThe same readable paragraph.\n\n## Second section\n\nThe same final paragraph.' },
    { type: 'table', columns: ['Area', 'Result'], rows: [['Rendering', 'Consistent']] },
    { type: 'callout', tone: 'warning', title: 'Shared warning', content: 'The same semantic treatment.' },
  ] },
};

async function installAudio(page: Page) {
  await page.route('**/api/v1/audio/jobs/source?**', route => {
    expect(new URL(route.request().url()).searchParams.get('content_ref')).toBe('chat-flow-review');
    return route.fulfill({ json: { jobs: [], supported: true, can_generate: true, can_estimate: true, can_cancel: true, detail: null } });
  });
  await page.route('**/api/v1/audio/jobs/estimates?**', route => route.fulfill({ json:
    ['shorter', 'default', 'longer'].map(length => ({ length, target_words: 100, seconds_min: 30, seconds_max: 60 })),
  }));
}

async function checkAudioMenu(page: Page) {
  const speaker = page.getByRole('button', { name: 'Listen to this document', exact: true });
  await expect(speaker).toHaveCount(1);
  await speaker.tap();
  const audio = page.getByRole('dialog', { name: 'Document audio', exact: true });
  await expect(audio.getByRole('tab', { name: 'Reading', exact: true })).toBeVisible();
  await expect(audio.getByRole('button', { name: 'Generate narration' })).toBeEnabled();
  await audio.getByRole('tab', { name: 'Podcast', exact: true }).tap();
  await expect(audio.getByRole('button', { name: 'Generate podcast' })).toBeEnabled();
  await expect(audio.getByRole('radio', { name: /Shorter/ })).toBeEnabled();
  await audio.getByRole('button', { name: 'Close audio controls' }).tap();
}

test('chat, fullscreen, Work embed, and Library share rendering and Reading/Podcast actions', async ({ page }) => {
  await installLibraryFixture(page);
  await installLibraryRoutes(page, [{ ...libraryCatalog[0], id: 'chat-flow-review', lineage_id: 'chat-flow-review', title: parityDocument.title, format: 'rich' }]);
  await page.route('**/api/v1/deliverables/chat-flow-review', route => route.fulfill({ json: parityDocument }));
  await page.route('**/api/v1/library/deliverables/chat-flow-review/content', route => route.fulfill({ json: parityDocument }));
  await installAudio(page);
  async function appearance() {
    await expect(page.getByTestId('rich-deliverable-body').getByRole('heading', { name: 'Reader parity', exact: true })).toBeVisible();
    await page.evaluate(() => document.fonts.ready);
    return page.getByTestId('rich-deliverable-body').evaluate(body => {
      return ['.rich-hero h1, .rich-hero h2', '.rich-markdown', '.rich-markdown h2', '.rich-callout', 'td'].map(selector => {
        const style = getComputedStyle(body.querySelector(selector)!);
        return [style.fontFamily, style.fontSize, style.lineHeight, style.color, style.backgroundColor, style.borderColor];
      });
    });
  }
  await page.goto('/chat-deliverable-fixture?favorites&api');
  const canonical = await appearance();
  await checkAudioMenu(page);
  await page.getByRole('button', { name: 'Open fullscreen' }).tap();
  expect(await appearance()).toEqual(canonical);
  await checkAudioMenu(page);
  await page.getByRole('button', { name: 'Close', exact: true }).tap();

  await page.goto('/chat-deliverable-fixture?favorites&api&work');
  await page.getByTitle('More document actions').tap();
  await page.getByRole('button', { name: 'Expand document', exact: true }).tap();
  expect(await appearance()).toEqual(canonical);
  await checkAudioMenu(page);

  await page.goto('/library/deliverable/chat-flow-review');
  await expect(page.getByTestId('resource-reader')).toBeVisible();
  expect(await appearance()).toEqual(canonical);
  await checkAudioMenu(page);
  await page.getByTitle('More document actions').tap();
  await expect(page.getByRole('button', { name: 'Add to favorites', exact: true })).toHaveText('Add to favorites');
  await expect(page.getByRole('button', { name: 'Copy document', exact: true })).toBeVisible();
  await page.screenshot({ path: test.info().outputPath('library-mobile.png') });
});

test('fullscreen has compact right-aligned actions and floating contents navigation', async ({ page }) => {
  await installLibraryFixture(page);
  await installLibraryRoutes(page, [{ ...libraryCatalog[0], id: 'chat-flow-review', lineage_id: 'chat-flow-review' }]);
  await page.goto('/chat-deliverable-fixture?favorites');
  await page.getByRole('button', { name: 'Open fullscreen' }).tap();
  const full = page.getByTestId('rich-deliverable-full-view');
  const chrome = full.getByTestId('rich-viewer-chrome');
  await expect(chrome.getByRole('button', { name: 'Copy document' })).toHaveCount(0);
  await expect(chrome.getByRole('button', { name: 'Open table of contents' })).toHaveCount(0);
  const contents = full.getByRole('button', { name: 'Open table of contents' });
  expect(await contents.boundingBox()).toMatchObject({ width: 44, height: 44 });
  for (const control of await chrome.locator('.document-icon-action').all()) {
    if (!await control.isVisible()) continue;
    expect(await control.boundingBox()).toMatchObject({ width: 44, height: 44 });
    expect(await control.locator('svg').boundingBox()).toMatchObject({ width: 20, height: 20 });
  }
  await expect(chrome.getByRole('button', { name: 'Add to favorites' })).toHaveCount(0);
  await chrome.getByTitle('More document actions').tap();
  const favorite = chrome.getByRole('button', { name: 'Add to favorites' });
  await expect(favorite).toHaveText('Add to favorites');
  await expect(favorite).toBeEnabled();
  await favorite.tap();
  await expect(chrome.getByRole('button', { name: 'Remove from favorites' })).toHaveAttribute('aria-pressed', 'true');
  await expect(chrome.getByRole('button', { name: 'Copy document' })).toBeVisible();
  await page.keyboard.press('Escape');
  await expect(chrome.getByRole('button', { name: 'Copy document' })).toHaveCount(0);
  await expect(full).toBeVisible();
  await full.getByRole('button', { name: 'Open table of contents' }).tap();
  await expect(page.getByRole('dialog', { name: 'Table of contents', exact: true })).toBeVisible();
  await page.getByRole('dialog', { name: 'Table of contents', exact: true }).getByRole('button', { name: 'Close table of contents', exact: true }).tap();
  await expect(full.getByRole('button', { name: 'Open table of contents' })).toBeVisible();
  await page.screenshot({ path: test.info().outputPath('fullscreen-mobile.png') });
});

test('download menu flips above a bottom-edge trigger', async ({ page }) => {
  await page.goto('/chat-deliverable-fixture');
  const trigger = page.getByTitle('Download document');
  await trigger.scrollIntoViewIfNeeded();
  await page.evaluate(() => {
    const main = document.querySelector('main')!;
    main.style.paddingBottom = '600px';
    const trigger = document.querySelector('.download-menu summary')!;
    main.scrollTop += trigger.getBoundingClientRect().bottom - (window.innerHeight - 12);
  });
  await trigger.tap();
  const menu = page.getByLabel('Download format');
  await expect(menu.getByRole('link', { name: 'PDF', exact: true })).toBeVisible();
  await expect.poll(async () => {
    const bounds = (await menu.boundingBox())!;
    const anchor = (await trigger.boundingBox())!;
    return bounds.y >= 8 && bounds.y + bounds.height <= anchor.y;
  }).toBe(true);
});

for (const fontSize of [14, 18]) {
  test(`mobile chat controls have identical geometry and an attached menu at ${fontSize}px text`, async ({ page }) => {
    await installLibraryFixture(page);
    await installLibraryRoutes(page, [{ ...libraryCatalog[0], id: 'chat-flow-review', lineage_id: 'chat-flow-review' }]);
    await page.goto('/chat-deliverable-fixture?favorites&chrome');
    await page.evaluate(fontSize => {
      document.documentElement.style.fontSize = `${fontSize}px`;
      document.documentElement.dataset.themeFamily = 'paper';
      document.documentElement.dataset.resolvedTheme = 'dark';
      document.documentElement.classList.add('dark');
    }, fontSize);
    const footer = page.getByTestId('chat-document-footer');
    await footer.scrollIntoViewIfNeeded();
    await expect(footer.getByRole('button', { name: 'Share link' })).toBeEnabled();
    const geometry = await footer.locator('.chat-document-actions').evaluate(nav => {
      return [...nav.querySelectorAll<HTMLElement>(':scope > button, :scope > details > summary')].map(control => {
        const box = control.getBoundingClientRect();
        const icon = control.querySelector('svg')!.getBoundingClientRect();
        return { name: control.getAttribute('aria-label'), width: box.width, height: box.height, iconWidth: icon.width, iconHeight: icon.height, center: box.top + box.height / 2 };
      });
    });
    expect(geometry).toHaveLength(5);
    for (const control of geometry) {
      expect(control, control.name ?? '').toMatchObject({ width: 44, height: 44, iconWidth: 20, iconHeight: 20 });
      expect(Math.abs(control.center - geometry[0].center)).toBeLessThanOrEqual(1);
    }
    await expect(footer.getByRole('button', { name: 'Add to favorites' })).toHaveCount(0);
    const trigger = footer.getByTitle('More document actions');
    await trigger.tap();
    const menu = footer.locator('.chat-document-menu');
    async function expectAttached() {
      await expect.poll(async () => {
        const anchor = (await trigger.boundingBox())!, popup = (await menu.boundingBox())!;
        const above = anchor.y - popup.y - popup.height;
        const below = popup.y - anchor.y - anchor.height;
        return Math.min(Math.abs(above - 6), Math.abs(below - 6));
      }).toBeLessThanOrEqual(1);
      const popup = (await menu.boundingBox())!;
      const main = (await page.locator('main.h-screen').boundingBox())!;
      expect(popup.y).toBeGreaterThanOrEqual(main.y);
      expect(popup.y + popup.height).toBeLessThanOrEqual(main.y + main.height);
    }
    await expectAttached();
    await expect(menu.getByRole('button', { name: 'Add to favorites' })).toHaveText('Add to favorites');
    await page.screenshot({ path: test.info().outputPath('chat-controls.png') });
    await page.locator('main.h-screen').evaluate(main => { main.scrollTop -= 18; });
    await expectAttached();
    await page.setViewportSize({ width: 390, height: 700 });
    // Resizing can scroll the trigger behind the composer. A popup without
    // its visible anchor must close instead of floating elsewhere onscreen.
    await expect(menu).not.toBeVisible();
    await trigger.scrollIntoViewIfNeeded();
    await trigger.tap();
    await expectAttached();
    await page.keyboard.press('Escape');
    await footer.getByTitle('Download document').tap();
    const downloads = footer.getByLabel('Download format');
    await expect.poll(async () => {
      const anchor = (await footer.getByTitle('Download document').boundingBox())!, popup = (await downloads.boundingBox())!;
      return Math.min(Math.abs(anchor.y - popup.y - popup.height - 6), Math.abs(popup.y - anchor.y - anchor.height - 6));
    }).toBeLessThanOrEqual(1);
  });
}

test('menu stays attached inside a scaled and translated chat ancestor', async ({ page }) => {
  await page.goto('/chat-deliverable-fixture?chrome');
  const footer = page.getByTestId('chat-document-footer');
  await footer.evaluate(element => {
    const document = element.closest<HTMLElement>('.rich-deliverable')!;
    document.style.transform = 'translateY(12px) scale(.9)';
    document.style.transformOrigin = 'right bottom';
  });
  const trigger = footer.getByTitle('More document actions');
  await trigger.scrollIntoViewIfNeeded();
  await trigger.tap();
  const menu = footer.locator('.chat-document-menu');
  await expect.poll(async () => {
    const anchor = (await trigger.boundingBox())!, popup = (await menu.boundingBox())!;
    return Math.abs(anchor.y - popup.y - popup.height - 6);
  }).toBeLessThanOrEqual(1);
});

test('horizontal-only attachment popup retains its vertical placement', async ({ page }) => {
  await page.goto('/chat-deliverable-fixture?save-error');
  const trigger = page.getByRole('button', { name: 'Download preview.txt', exact: true });
  await trigger.tap();
  const message = page.getByText('The download URL could not be loaded. Please try again.', { exact: true });
  await expect(message).toBeVisible();
  await expect.poll(async () => (await message.boundingBox())!.x).toBeGreaterThanOrEqual(8);
  const popup = (await message.boundingBox())!, anchor = (await trigger.boundingBox())!;
  expect(popup.x + popup.width).toBeLessThanOrEqual(382);
  expect(popup.y).toBeGreaterThanOrEqual(anchor.y + anchor.height);
  expect(popup.y - anchor.y - anchor.height).toBeLessThanOrEqual(5);
});
