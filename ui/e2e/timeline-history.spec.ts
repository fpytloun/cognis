import { expect, test, type Locator } from '@playwright/test';

interface HistoryFixture {
  snapshots: number;
  historyRequests: number;
  releaseHistory: (() => void) | null;
  append(): void;
  older(): Promise<unknown>;
  closeScoped(): void;
  closeFull(): void;
  changeConversation(): void;
  counts(): { entries: number; retained: number };
  owner(): string | null;
  unexpectedRequests: string[];
  syncRequests: number;
  releaseSync: (() => void) | null;
}
declare global { interface Window {
  historyFixture: HistoryFixture;
  releaseRestorationFrames: () => void;
} }

test('cached managed history stays visible during catch-up and reconciles only delegate compactions', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.route('**/*', route => {
    const url = new URL(route.request().url());
    if (url.origin !== 'http://127.0.0.1:4193' || url.pathname.startsWith('/api/')) return route.abort();
    return route.continue();
  });
  await page.goto('/history.html?cached-managed&hold-sync');
  const viewport = page.getByTestId('scoped-timeline-viewport');
  await expect(viewport).toBeVisible();
  await expect.poll(() => page.evaluate(() => window.historyFixture.syncRequests)).toBe(1);
  await expect(viewport).not.toHaveClass(/invisible/);
  expect(await page.evaluate(() => window.historyFixture.releaseSync !== null)).toBe(true);
  await page.evaluate(() => window.historyFixture.releaseSync?.());
  await expect(viewport.locator('[data-timeline-item-id="compaction:cached-own"]')).toBeVisible();
  await expect(viewport.locator('[data-timeline-item-id="compaction:leaked-one"]')).toHaveCount(0);
  await expect(viewport.locator('[data-timeline-item-id="compaction:leaked-two"]')).toHaveCount(0);
  expect(await page.evaluate(() => window.historyFixture.unexpectedRequests)).toEqual([]);
});

async function anchor(viewport: Locator) {
  return viewport.evaluate(async v => {
    await new Promise(requestAnimationFrame);
    await new Promise(requestAnimationFrame);
    const top = v.getBoundingClientRect().top;
    const row = [...v.querySelectorAll<HTMLElement>('[data-timeline-row-key]')]
      .find(node => node.getBoundingClientRect().bottom > top)!;
    return { key: row.dataset.timelineRowKey!, offset: row.getBoundingClientRect().top - top };
  });
}
async function expectAnchor(viewport: Locator, before: { key: string; offset: number }) {
  const offset = await viewport.evaluate(async (v, key) => {
    await new Promise(requestAnimationFrame);
    await new Promise(requestAnimationFrame);
    const row = [...v.querySelectorAll<HTMLElement>('[data-timeline-row-key]')]
      .find(node => node.dataset.timelineRowKey === key);
    return row ? row.getBoundingClientRect().top - v.getBoundingClientRect().top : null;
  }, before.key);
  expect(offset).not.toBeNull();
  expect(Math.abs(offset! - before.offset)).toBeLessThanOrEqual(2);
}

for (const mode of ['visible', 'hidden', 'returning', 'teardown', 'new-conversation'] as const) {
test(`shared non-null-owner full/scoped history preserves independent anchors; parent=${mode}`, async ({ page }) => {
  const hidden = mode !== 'visible';
  const suspended = mode === 'returning' || mode === 'teardown' || mode === 'new-conversation';
  await page.setViewportSize({ width: 390, height: 844 });
  const escaped: string[] = [];
  await page.route('**/*', route => {
    const url = new URL(route.request().url());
    if (url.origin !== 'http://127.0.0.1:4193' || url.pathname.startsWith('/api/')) {
      escaped.push(url.href);
      return route.abort();
    }
    return route.continue();
  });
  await page.goto('/history.html');
  const full = page.getByTestId('full-surface').getByTestId('timeline-viewport');
  const scoped = page.getByTestId('scoped-surface').getByTestId('scoped-timeline-viewport');
  await expect(full.getByText('Synthetic history 199', { exact: true })).toBeAttached();
  await expect(scoped.getByText('Synthetic history 199', { exact: true })).toBeAttached();
  await expect(scoped).toBeVisible();
  await expect.poll(() => full.evaluate(e => e.scrollHeight - e.scrollTop - e.clientHeight)).toBeLessThanOrEqual(2);
  // First boot constructs the tail window; it must not retain the empty
  // pre-snapshot range as if it were an established reading position.
  await expect(full.getByText('Synthetic history 0', { exact: true })).not.toBeAttached();
  await expect(full.locator('[data-timeline-row-key]')).toHaveCount(100);
  expect(await page.evaluate(() => window.historyFixture.owner())).not.toBeNull();
  expect(await page.evaluate(() => window.historyFixture.counts())).toEqual({ entries: 1, retained: 1 });
  await full.evaluate(v => {
    const row = [...v.querySelectorAll<HTMLElement>('[data-timeline-row-key]')]
      .find(node => node.textContent?.includes('Synthetic history 120'))!;
    v.scrollTop += row.getBoundingClientRect().top - v.getBoundingClientRect().top - 50;
    v.dispatchEvent(new WheelEvent('wheel', { deltaY: -1 }));
  });
  await expect(full.getByRole('button', { name: 'Scroll to bottom', exact: true })).toBeAttached();
  let parentAnchor: { key: string; offset: number } | null = null;
  const keys = () => full.locator('[data-timeline-row-key]').evaluateAll(rows =>
    rows.map(row => (row as HTMLElement).dataset.timelineRowKey));
  const retainedKeys = await keys();
  if (hidden) {
    await full.locator('[aria-label="Background hook"]').evaluate(element => {
      element.querySelector<HTMLButtonElement>('button')!.click();
    });
    parentAnchor = await anchor(full);
    await full.locator('[aria-label="Background hook"]').getByRole('button', { name: 'Open session' })
      .evaluate(element => (element as HTMLButtonElement).click());
    await expect(page.getByTestId('child-header')).toBeVisible();
  }
  if (suspended) {
    await page.evaluate(() => {
      const original = window.requestAnimationFrame;
      const cancel = window.cancelAnimationFrame;
      const queued = new Map<number, FrameRequestCallback>();
      let id = 1_000_000;
      window.requestAnimationFrame = callback => { queued.set(++id, callback); return id; };
      window.cancelAnimationFrame = key => { if (!queued.delete(key)) cancel(key); };
      window.releaseRestorationFrames = () => {
        window.requestAnimationFrame = original;
        window.cancelAnimationFrame = cancel;
        for (const callback of queued.values()) original(callback);
        queued.clear();
      };
    });
    await page.getByRole('button', { name: 'Back to parent conversation' }).evaluate(e => (e as HTMLButtonElement).click());
    await expect(page.getByTestId('child-header')).not.toBeAttached();
  }
  for (let pageIndex = 1; pageIndex <= 2; pageIndex++) {
    await scoped.evaluate(v => {
      v.scrollTop = 0;
      v.dispatchEvent(new WheelEvent('wheel', { deltaY: -1 }));
    });
    await page.waitForFunction(() => window.historyFixture.releaseHistory !== null);
    expect(await page.evaluate(() => window.historyFixture.historyRequests)).toBe(pageIndex);
    const fullBefore = hidden ? parentAnchor! : await anchor(full);
    const scopedBefore = suspended ? null : await anchor(scoped);
    await page.evaluate(() => { window.historyFixture.append(); });
    if (!hidden) await expectAnchor(full, fullBefore);
    if (scopedBefore) await expectAnchor(scoped, scopedBefore);
    if (pageIndex === 2) {
      // Same-page second consumer keeps the fetch alive while its originating
      // scoped view closes; one network page still applies exactly once.
      await page.evaluate(() => { void window.historyFixture.older(); window.historyFixture.closeScoped(); });
    }
    await page.evaluate(() => window.historyFixture.releaseHistory!());
    await page.waitForFunction(() => window.historyFixture.releaseHistory === null);
    if (suspended) expect(await keys()).toEqual(retainedKeys);
    await expect(full.getByText(`Synthetic live update ${pageIndex}`, { exact: true })).toBeAttached();
    if (!hidden) await expectAnchor(full, fullBefore);
    if (pageIndex === 1) {
      await expect(scoped.getByText('Synthetic history -60', { exact: true })).toBeAttached();
      if (scopedBefore) await expectAnchor(scoped, scopedBefore);
    }
  }
  if (mode === 'new-conversation') {
    await page.evaluate(() => window.historyFixture.changeConversation());
    await expect(full.getByText('New conversation only', { exact: true })).toBeAttached();
    await page.evaluate(() => window.releaseRestorationFrames());
    await expect(full.locator('[data-timeline-row-key]')).toHaveCount(1);
    await expect.poll(() => full.evaluate(e => e.scrollTop)).toBe(0);
  } else if (mode === 'teardown') {
    await page.evaluate(() => window.historyFixture.closeFull());
    await expect(full).not.toBeAttached();
    await page.evaluate(() => window.releaseRestorationFrames());
    await expect(full).not.toBeAttached();
  } else if (suspended) {
    await page.evaluate(() => window.releaseRestorationFrames());
    await expectAnchor(full, parentAnchor!);
  } else if (hidden) {
    await page.getByRole('button', { name: 'Back to parent conversation' }).click();
    await expect(page.getByTestId('child-header')).not.toBeAttached();
    await expectAnchor(full, parentAnchor!);
    await expect(full.getByText('Synthetic live update 2', { exact: true })).toBeAttached();
  }
  expect(await page.evaluate(() => window.historyFixture.historyRequests)).toBe(2);
  expect(await page.evaluate(() => window.historyFixture.snapshots)).toBe(1);
  expect(await page.evaluate(() => window.historyFixture.unexpectedRequests)).toEqual([]);
  expect(escaped).toEqual([]);
});
}
