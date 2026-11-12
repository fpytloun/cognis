import { expect, test, type Locator, type Page } from '@playwright/test';

const pageErrors = new WeakMap<Page, string[]>();
test.beforeEach(({ page }) => {
  const errors: string[] = [];
  pageErrors.set(page, errors);
  page.on('pageerror', error => errors.push(error.message));
});
test.afterEach(({ page }) => {
  expect(pageErrors.get(page)).toEqual([]);
});

async function edgeReading(viewport: Locator, edge: 'top' | 'bottom') {
  return viewport.evaluate(async (node, direction) => {
    await new Promise(requestAnimationFrame);
    await new Promise(requestAnimationFrame);
    node.scrollTop = direction === 'top' ? 0 : node.scrollHeight - node.clientHeight;
    const rect = node.getBoundingClientRect();
    const leaf = Array.from(node.querySelectorAll<HTMLElement>('[data-timeline-item-id]')).find(leaf => {
      const bounds = leaf.getBoundingClientRect();
      return bounds.height > 0 && bounds.bottom > rect.top && bounds.top < rect.bottom;
    });
    return leaf ? { id: leaf.dataset.timelineItemId!, top: leaf.getBoundingClientRect().top - rect.top } : null;
  }, edge);
}

async function expectReading(viewport: Locator, reading: { id: string; top: number } | null) {
  expect(reading).not.toBeNull();
  await expect.poll(async () => viewport.evaluate((node, expected) => {
    const leaf = Array.from(node.querySelectorAll<HTMLElement>('[data-timeline-item-id]'))
      .find(leaf => leaf.dataset.timelineItemId === expected!.id);
    return leaf ? Math.abs(leaf.getBoundingClientRect().top - node.getBoundingClientRect().top - expected!.top) : Infinity;
  }, reading)).toBeLessThanOrEqual(2);
}

for (const nativeAnchoring of ['auto', 'none']) {
  test(`main chat preserves content across window edges and network backfill (${nativeAnchoring})`, async ({ page }) => {
    await page.goto('/history.html?full-only&scenario=scroll-messages');
    const viewport = page.getByTestId('full-surface').getByTestId('timeline-viewport');
    await expect(viewport).toBeVisible();
    await expect(viewport.locator('[data-timeline-item-id]').last()).toContainText('Synthetic history 799');
    await viewport.evaluate((node, anchor) => { node.style.overflowAnchor = anchor; }, nativeAnchoring);
    await expect.poll(() => viewport.evaluate(node => node.scrollHeight - node.clientHeight - node.scrollTop))
      .toBeLessThanOrEqual(2);

    for (let pageIndex = 0; pageIndex < 11; pageIndex++) {
      const before = await viewport.locator('[data-timeline-item-id]').first().getAttribute('data-timeline-item-id');
      const reading = await edgeReading(viewport, 'top');
      await viewport.dispatchEvent('wheel', { deltaY: -5 });
      await expect.poll(() => viewport.locator('[data-timeline-item-id]').first().getAttribute('data-timeline-item-id'))
        .not.toBe(before);
      await test.step(`upward page ${pageIndex}: ${reading?.id}`, () => expectReading(viewport, reading));
      expect(await viewport.locator('[data-timeline-item-id]').count()).toBeLessThanOrEqual(450);
    }
    const stats = await page.evaluate(() => (window as unknown as {
      historyFixture: { historyRequests: number; maxBackfillInFlight: number };
    }).historyFixture);
    expect(stats.historyRequests).toBe(3);
    expect(stats.maxBackfillInFlight).toBe(1);

    for (let pageIndex = 0; pageIndex < 5; pageIndex++) {
      const before = await viewport.locator('[data-timeline-item-id]').last().getAttribute('data-timeline-item-id');
      const reading = await edgeReading(viewport, 'bottom');
      await viewport.dispatchEvent('wheel', { deltaY: 5 });
      await expect.poll(() => viewport.locator('[data-timeline-item-id]').last().getAttribute('data-timeline-item-id'))
        .not.toBe(before);
      await test.step(`downward page ${pageIndex}: ${reading?.id}`, () => expectReading(viewport, reading));
      expect(await viewport.locator('[data-timeline-item-id]').count()).toBeLessThanOrEqual(450);
    }
  });
}

test('giant collapsed activity preserves its header in both directions with bounded rendering', async ({ page }) => {
  await page.goto('/history.html?full-only&scenario=scroll-collapsed');
  const viewport = page.getByTestId('full-surface').getByTestId('timeline-viewport');
  await expect(viewport).toBeVisible();
  await expect.poll(() => viewport.locator('[data-timeline-row-key]').first().getAttribute('data-timeline-item-ids')
    .then(ids => JSON.parse(ids ?? '[]').length)).toBe(450);
  await viewport.evaluate(node => { node.style.overflowAnchor = 'none'; });
  for (const direction of [-1, 1]) for (let step = 0; step < 4; step++) {
    const reading = await viewport.evaluate(async (node, direction) => {
      await new Promise(requestAnimationFrame);
      await new Promise(requestAnimationFrame);
      node.scrollTop = direction < 0 ? 0 : node.scrollHeight - node.clientHeight;
      const row = node.querySelector<HTMLElement>('[data-timeline-row-key]')!;
      return { ids: row.dataset.timelineItemIds!, top: row.getBoundingClientRect().top - node.getBoundingClientRect().top };
    }, direction);
    await viewport.dispatchEvent('wheel', { deltaY: direction * 5 });
    await expect.poll(() => viewport.locator('[data-timeline-row-key]').first().getAttribute('data-timeline-item-ids'))
      .not.toBe(reading.ids);
    await expect.poll(() => viewport.evaluate((node, reading) => {
      const row = node.querySelector<HTMLElement>('[data-timeline-row-key]')!;
      const ids = new Set<string>(JSON.parse(reading.ids));
      const next: string[] = JSON.parse(row.dataset.timelineItemIds!);
      if (!next.some(id => ids.has(id))) return Infinity;
      return Math.abs(row.getBoundingClientRect().top - node.getBoundingClientRect().top - reading.top);
    }, reading)).toBeLessThanOrEqual(2);
    const members = await viewport.locator('[data-timeline-row-key]').first().getAttribute('data-timeline-item-ids');
    expect(JSON.parse(members!).length).toBeLessThanOrEqual(450);
  }
});

test('expanded tool content survives a group re-key during upward paging', async ({ page }) => {
  await page.goto('/history.html?full-only&scenario=scroll-groups');
  const viewport = page.getByTestId('full-surface').getByTestId('timeline-viewport');
  await expect(viewport).toBeVisible();
  const first = viewport.locator('[data-timeline-row-key]').first();
  await expect(first).toBeAttached();
  await first.locator('button[aria-expanded]').first().dispatchEvent('click');
  await expect.poll(() => viewport.evaluate(node => node.getAnimations({ subtree: true }).length)).toBe(0);
  const previous = await first.getAttribute('data-timeline-item-ids');
  const reading = await edgeReading(viewport, 'top');
  await viewport.dispatchEvent('wheel', { deltaY: -5 });
  await expect.poll(() => viewport.locator('[data-timeline-row-key]').first().getAttribute('data-timeline-item-ids'))
    .not.toBe(previous);
  await expectReading(viewport, reading);
  const expanded = viewport.locator(`[data-timeline-item-id="${reading!.id}"]`).locator('xpath=ancestor::*[@data-timeline-row-key][1]');
  await expect(expanded.locator('button[aria-expanded]').first()).toHaveAttribute('aria-expanded', 'true');
});

test('streaming and delayed layout preserve history reading; jump-to-latest resumes bounded tail follow', async ({ page }) => {
  await page.goto('/history.html?full-only&scenario=scroll-messages');
  const viewport = page.getByTestId('full-surface').getByTestId('timeline-viewport');
  await expect(viewport.locator('[data-timeline-item-id]').last()).toContainText('Synthetic history 799');
  await viewport.evaluate(node => { node.style.overflowAnchor = 'none'; });
  await edgeReading(viewport, 'top');
  await viewport.dispatchEvent('wheel', { deltaY: -5 });
  await expect(viewport.locator('[data-timeline-item-id]').first()).toHaveAttribute('data-timeline-item-id', 'history:650');
  const reading = await viewport.evaluate(async node => {
    await new Promise(requestAnimationFrame);
    await new Promise(requestAnimationFrame);
    const rect = node.getBoundingClientRect();
    const leaf = Array.from(node.querySelectorAll<HTMLElement>('[data-timeline-item-id]'))
      .find(leaf => leaf.getBoundingClientRect().bottom > rect.top)!;
    return { id: leaf.dataset.timelineItemId!, top: leaf.getBoundingClientRect().top - rect.top };
  });
  await page.evaluate(() => (window as unknown as { historyFixture: { append: () => void } }).historyFixture.append());
  await expectReading(viewport, reading);
  await viewport.evaluate(node => {
    node.querySelector<HTMLElement>('[data-timeline-item-id]')!.style.paddingTop = '120px';
  });
  await expectReading(viewport, reading);
  await page.getByRole('button', { name: 'Scroll to bottom', exact: true }).click();
  await expect(viewport.locator('[data-timeline-item-id]').last()).toContainText('Synthetic history 801');
  await expect.poll(() => viewport.evaluate(node => node.scrollHeight - node.clientHeight - node.scrollTop))
    .toBeLessThanOrEqual(2);
  expect(await viewport.locator('[data-timeline-item-id]').count()).toBeLessThanOrEqual(100);
});

test('a delayed history response preserves the newer reading position after the user keeps scrolling', async ({ page }) => {
  await page.goto('/history.html?full-only&scenario=scroll-messages');
  const viewport = page.getByTestId('full-surface').getByTestId('timeline-viewport');
  await expect(viewport.locator('[data-timeline-item-id]').last()).toContainText('Synthetic history 799');
  await viewport.evaluate(node => { node.style.overflowAnchor = 'none'; });
  for (let step = 0; step < 8; step++) {
    const before = await viewport.locator('[data-timeline-item-id]').first().getAttribute('data-timeline-item-id');
    await edgeReading(viewport, 'top');
    await viewport.dispatchEvent('wheel', { deltaY: -5 });
    await expect.poll(() => viewport.locator('[data-timeline-item-id]').first().getAttribute('data-timeline-item-id'))
      .not.toBe(before);
  }
  await expect(viewport.locator('[data-timeline-item-id]').first()).toHaveAttribute('data-timeline-item-id', 'history:300');
  await page.evaluate(() => {
    (window as unknown as { historyFixture: { holdScrollingHistory: boolean } }).historyFixture.holdScrollingHistory = true;
  });
  await edgeReading(viewport, 'top');
  await viewport.dispatchEvent('wheel', { deltaY: -5 });
  await expect.poll(() => page.evaluate(() =>
    (window as unknown as { historyFixture: { backfillInFlight: number } }).historyFixture.backfillInFlight)).toBe(1);
  await viewport.evaluate(node => { node.scrollTop = 220; });
  const reading = await viewport.evaluate(async node => {
    await new Promise(requestAnimationFrame);
    await new Promise(requestAnimationFrame);
    const rect = node.getBoundingClientRect();
    const leaf = Array.from(node.querySelectorAll<HTMLElement>('[data-timeline-item-id]'))
      .find(leaf => leaf.getBoundingClientRect().bottom > rect.top)!;
    return { id: leaf.dataset.timelineItemId!, top: leaf.getBoundingClientRect().top - rect.top };
  });
  await page.evaluate(() => (window as unknown as { historyFixture: { releaseHistory: () => void } }).historyFixture.releaseHistory());
  await expect(viewport.locator('[data-timeline-item-id]').first()).toHaveAttribute('data-timeline-item-id', 'history:200');
  await expectReading(viewport, reading);
});

test('late layout never rewinds input delivered during the programmatic-scroll guard', async ({ page }) => {
  await page.addInitScript(() => {
    const descriptor = Object.getOwnPropertyDescriptor(Element.prototype, 'scrollTop')!;
    const race = { armed: false, expected: null as number | null };
    Object.assign(window, { historyInputRace: race });
    Object.defineProperty(Element.prototype, 'scrollTop', {
      ...descriptor,
      set(value) {
        descriptor.set!.call(this, value);
        if (race.armed && value > 100 && this.getAttribute('data-testid') === 'timeline-viewport') {
          race.armed = false;
          queueMicrotask(() => {
            descriptor.set!.call(this, descriptor.get!.call(this) + 200);
            race.expected = descriptor.get!.call(this);
            this.querySelector('[data-timeline-item-id]').style.paddingTop = '120px';
          });
        }
      },
    });
  });
  await page.goto('/history.html?full-only&scenario=scroll-messages');
  const viewport = page.getByTestId('full-surface').getByTestId('timeline-viewport');
  await expect(viewport.locator('[data-timeline-item-id]').last()).toContainText('Synthetic history 799');
  await viewport.evaluate(node => { node.style.overflowAnchor = 'none'; });
  await edgeReading(viewport, 'top');
  await page.evaluate(() => { (window as unknown as { historyInputRace: { armed: boolean } }).historyInputRace.armed = true; });
  await viewport.dispatchEvent('wheel', { deltaY: -5 });
  await expect.poll(() => page.evaluate(() =>
    (window as unknown as { historyInputRace: { expected: number | null } }).historyInputRace.expected)).not.toBeNull();
  await expect.poll(() => viewport.evaluate(async node => {
    await new Promise(requestAnimationFrame);
    await new Promise(requestAnimationFrame);
    const expected = (window as unknown as { historyInputRace: { expected: number } }).historyInputRace.expected;
    return Math.abs(node.scrollTop - expected);
  })).toBeLessThanOrEqual(1);
});

test('keyboard paging works at a non-scrollable collapsed history boundary', async ({ page }) => {
  await page.goto('/history.html?full-only&scenario=scroll-collapsed');
  const viewport = page.getByTestId('full-surface').getByTestId('timeline-viewport');
  await expect.poll(() => viewport.locator('[data-timeline-row-key]').first().getAttribute('data-timeline-item-ids')
    .then(ids => JSON.parse(ids ?? '[]').length)).toBe(450);
  const before = await viewport.locator('[data-timeline-row-key]').first().getAttribute('data-timeline-item-ids');
  await viewport.dispatchEvent('keydown', { key: 'PageUp' });
  await expect.poll(() => viewport.locator('[data-timeline-row-key]').first().getAttribute('data-timeline-item-ids'))
    .not.toBe(before);
});
