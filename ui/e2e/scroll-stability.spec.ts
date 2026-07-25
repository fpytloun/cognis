/**
 * L3 Playwright scroll-stability tests (Symptom 4).
 *
 * Verifies the single-scroll-driver behaviour during streaming:
 *  - The tail stays pinned to the bottom as content streams in (no jitter,
 *    no mid-stream un-pin).
 *  - A user who scrolls up during streaming is NOT yanked back to the bottom
 *    by incoming tokens (focus/scroll preservation).
 *
 * Pre-fix, two independent scroll drivers (per-patch scrollToBottom + the
 * ResizeObserver) raced each other writing scrollTop across rAF phases,
 * producing jitter and occasional loss of the user's scroll position. The fix
 * makes the ResizeObserver the single driver for streaming growth.
 *
 * Prerequisites: make e2e-up && make e2e-seed
 * Run: cd ui && npx playwright test e2e/scroll-stability.spec.ts
 */

import { test } from '@playwright/test';
import {
  expect,
  login,
  injectScenario,
  clearScenario,
  openOrCreateConversation,
  sendMessage,
  waitForTurnComplete,
} from './helpers';

const VIEWPORT = '[data-testid="timeline-viewport"]';
const SCROLL_TO_BOTTOM = '[data-testid="timeline-viewport-scroll-to-bottom"]';
const SCROLL_TO_ACTIVE_START = '[data-testid="timeline-viewport-scroll-to-active-start"]';

test.beforeEach(async ({ page }) => {
  await login(page);
  await openOrCreateConversation(page);
});

test.afterEach(async () => {
  await clearScenario().catch(() => {});
});

test('long-streaming-response: tail stays pinned during streaming burst', async ({ page }) => {
  test.setTimeout(120_000);
  await injectScenario('long-streaming-response');
  await sendMessage(page, 'scenario:long-streaming-response');

  const viewport = page.locator(VIEWPORT);
  await expect(viewport).toBeVisible();
  await expect(page.locator('[data-streaming="true"]')).not.toHaveCount(0);
  await expect(page.getByTestId('timeline-viewport-navigation-cluster')).toHaveCount(0);

  // While streaming, repeatedly sample the distance from the bottom. With a
  // single scroll driver and the tail pinned, the viewport must remain at (or
  // within a small threshold of) the bottom — no jitter spikes.
  const samples: number[] = [];
  const deadline = Date.now() + 8000;
  while (Date.now() < deadline) {
    const distance = await viewport.evaluate((el) => {
      const node = el as HTMLElement;
      return node.scrollHeight - node.scrollTop - node.clientHeight;
    });
    samples.push(distance);
    // Stop early once streaming finished
    const stillStreaming = await page
      .locator('[data-streaming="true"]')
      .count()
      .then((c) => c > 0)
      .catch(() => false);
    if (!stillStreaming && samples.length > 5) break;
    await page.waitForTimeout(120);
  }

  await waitForTurnComplete(page, 90_000);

  // Sustained drift while pinned is a bug, but a few samples can observe
  // layout before the ResizeObserver/rAF scroll correction lands.
  const THRESHOLD_PX = 80;
  let consecutiveOvershoot = 0;
  let maxConsecutiveOvershoot = 0;
  for (const sample of samples) {
    if (sample > THRESHOLD_PX) {
      consecutiveOvershoot += 1;
      maxConsecutiveOvershoot = Math.max(maxConsecutiveOvershoot, consecutiveOvershoot);
    } else {
      consecutiveOvershoot = 0;
    }
  }
  expect(
    maxConsecutiveOvershoot,
    `tail drifted from bottom during streaming; distances=${JSON.stringify(samples)}`,
  ).toBeLessThanOrEqual(4);

  // After completion the viewport must be at the bottom.
  await page.waitForFunction(
    async (threshold) => {
      await new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve)));
      const node = document.querySelector('[data-testid="timeline-viewport"]');
      if (!(node instanceof HTMLElement)) return false;
      return node.scrollHeight - node.scrollTop - node.clientHeight <= threshold;
    },
    THRESHOLD_PX,
    { timeout: 5_000 },
  );
});

test('single-phase-stream: short response never shows an up/start control', async ({ page }) => {
  test.setTimeout(90_000);
  await page.setViewportSize({ width: 390, height: 844 });
  await injectScenario('single-phase-stream');
  await sendMessage(page, 'scenario:single-phase-stream');
  await waitForTurnComplete(page, 60_000);

  await expect(page.locator(SCROLL_TO_ACTIVE_START)).toHaveCount(0);
});

test('long-streaming-response: manual scroll-up is preserved during streaming', async ({
  page,
}) => {
  test.setTimeout(120_000);
  await injectScenario('long-streaming-response');
  await sendMessage(page, 'scenario:long-streaming-response');

  const viewport = page.locator(VIEWPORT);
  await expect(viewport).toBeVisible();
  await expect(page.locator('[data-streaming="true"]')).not.toHaveCount(0);

  // Wait until some content has streamed in so there is room to scroll up.
  await page.waitForTimeout(600);

  // User scrolls up (wheel up) mid-stream.
  await viewport.evaluate((el) => {
    (el as HTMLElement).scrollTop = 0;
    el.dispatchEvent(new WheelEvent('wheel', { deltaY: -400, bubbles: true }));
  });

  const scrolledTop = await viewport.evaluate((el) => (el as HTMLElement).scrollTop);

  // Continue letting tokens stream; the view must NOT be yanked back down.
  await page.waitForTimeout(1500);

  const afterStreamingTop = await viewport.evaluate((el) => (el as HTMLElement).scrollTop);

  // The scroll position must remain near where the user left it (not snapped
  // to the bottom). Pre-fix, per-patch scrollToBottom yanked it back.
  expect(
    Math.abs(afterStreamingTop - scrolledTop),
    `view was yanked back during streaming (from ${scrolledTop} to ${afterStreamingTop})`,
  ).toBeLessThanOrEqual(120);

  // The "scroll to bottom" affordance should be present while scrolled up.
  await expect(page.locator(SCROLL_TO_BOTTOM)).toBeVisible();

  await expect(page.locator('[data-streaming="true"]')).toHaveCount(0, { timeout: 90_000 });
});

test('long-streaming-response: contextual start pauses live tail and bottom reactivates it', async ({
  page,
}) => {
  test.setTimeout(120_000);
  await page.setViewportSize({ width: 390, height: 844 });
  await injectScenario('long-streaming-response');
  await sendMessage(page, 'scenario:long-streaming-response');

  const viewport = page.locator(VIEWPORT);
  await expect(viewport).toBeVisible();
  await page.waitForFunction(() => {
    const node = document.querySelector('[data-testid="timeline-viewport"]');
    return node instanceof HTMLElement && node.scrollHeight - node.clientHeight > 200;
  });
  await viewport.evaluate((element) => {
    const node = element as HTMLElement;
    node.scrollTop = node.scrollHeight - node.clientHeight;
    node.dispatchEvent(new Event('scroll', { bubbles: true }));
    node.dispatchEvent(new WheelEvent('wheel', { deltaY: -240, bubbles: true }));
  });

  const jumpToStart = page.locator(SCROLL_TO_ACTIVE_START);
  await expect(jumpToStart).toBeVisible();
  await expect(page.getByTestId('timeline-viewport-navigation-cluster').getByRole('button')).toHaveCount(2);
  await jumpToStart.focus();
  await expect(jumpToStart).toBeFocused();
  await page.keyboard.press('Enter');
  const startTop = await viewport.evaluate((element) => (element as HTMLElement).scrollTop);

  // Streaming and ResizeObserver growth must preserve the host-owned detached
  // state instead of racing the contextual programmatic scroll.
  const positions: number[] = [];
  for (let index = 0; index < 8; index += 1) {
    await page.waitForTimeout(150);
    positions.push(await viewport.evaluate((element) => (element as HTMLElement).scrollTop));
  }
  expect(
    Math.max(...positions.map((position) => Math.abs(position - startTop))),
    `jump-to-start oscillated during growth: start=${startTop}, positions=${positions.join(',')}`,
  ).toBeLessThanOrEqual(120);
  await expect(page.locator(SCROLL_TO_BOTTOM)).toBeVisible();

  await page.locator(SCROLL_TO_BOTTOM).click();
  await page.waitForFunction(() => {
    const node = document.querySelector('[data-testid="timeline-viewport"]');
    if (!(node instanceof HTMLElement)) return false;
    return node.scrollHeight - node.scrollTop - node.clientHeight <= 80;
  });
  await page.waitForTimeout(500);
  const distanceAfterGrowth = await viewport.evaluate((element) => {
    const node = element as HTMLElement;
    return node.scrollHeight - node.scrollTop - node.clientHeight;
  });
  expect(distanceAfterGrowth).toBeLessThanOrEqual(80);
  await expect(page.locator(SCROLL_TO_BOTTOM)).toHaveCount(0);
  await expect(page.getByTestId('timeline-viewport-navigation-cluster')).toHaveCount(0);

  await waitForTurnComplete(page, 90_000);
});

test('long-rich-deliverable: mobile message controls open and restore the TOC drawer', async ({
  page,
}, testInfo) => {
  test.setTimeout(120_000);
  await page.setViewportSize({ width: 390, height: 844 });
  await injectScenario('long-rich-deliverable');
  await sendMessage(page, 'scenario:long-rich-deliverable');
  await waitForTurnComplete(page, 90_000);

  const viewport = page.locator(VIEWPORT);
  await expect(viewport.locator('[data-has-contextual-toc="true"]')).toBeVisible();
  await expect(viewport.getByTestId('rich-deliverable-toc')).toHaveCount(1);
  await expect(viewport.getByRole('button', { name: 'Open table of contents' })).toHaveCount(1);
  await expect(viewport.getByText('Contents', { exact: true })).toBeHidden();
  await viewport.evaluate((element) => {
    const node = element as HTMLElement;
    node.scrollTop = Math.max(0, node.scrollHeight - node.clientHeight - 120);
    node.dispatchEvent(new Event('scroll', { bubbles: true }));
  });

  const tocTrigger = viewport.locator('[data-has-contextual-toc="true"]').last().locator('..').getByRole('button', { name: 'Open table of contents' });
  const messageControls = page.getByRole('navigation', { name: 'Message navigation' });
  await expect(tocTrigger).toBeVisible();
  await expect(messageControls).toBeVisible();
  await expect(messageControls.getByRole('button')).toHaveCount(2);
  await expect(
    tocTrigger,
  ).toBeVisible();
  await tocTrigger.focus();
  await tocTrigger.click();
  const toc = page.locator('nav[aria-label="Table of contents"][role="dialog"]');
  await expect(toc).toBeVisible();
  await expect(toc.getByRole('button', { name: 'Section 1', exact: true })).toBeVisible();
  await expect(toc.getByRole('button', { name: 'Section 10', exact: true })).toBeVisible();
  await expect(page.getByTestId('rich-deliverable-toc')).toHaveCount(1);
  await expect(toc.getByRole('button', { name: 'Close table of contents' })).toBeFocused();
  await expect(messageControls).toBeHidden();
  expect(await page.evaluate(() => document.documentElement.scrollWidth))
    .toBeLessThanOrEqual(390);

  const screenshotPath = testInfo.outputPath('chat-rich-toc-drawer-mobile-390.png');
  await page.screenshot({ path: screenshotPath, fullPage: true });
  await testInfo.attach('chat-rich-toc-drawer-mobile-390.png', {
    path: screenshotPath,
    contentType: 'image/png',
  });

  await page.getByTestId('rich-toc-backdrop').click({ position: { x: 8, y: 8 } });
  await expect(toc).toHaveCount(0);
  await expect(tocTrigger).toBeFocused();

  await tocTrigger.click();
  await expect(toc).toBeVisible();
  await page.keyboard.press('Escape');
  await expect(toc).toHaveCount(0);
  await expect(tocTrigger).toBeFocused();
});

test('long-rich-deliverable: desktop keeps a compact sticky TOC without overflow', async ({ page }) => {
  test.setTimeout(120_000);
  await page.setViewportSize({ width: 1440, height: 1000 });
  await injectScenario('long-rich-deliverable');
  await sendMessage(page, 'scenario:long-rich-deliverable');
  await waitForTurnComplete(page, 90_000);

  const viewport = page.locator(VIEWPORT);
  const deliverable = page.locator('[data-kind="assistant_deliverable"]').last();
  const toc = deliverable.getByRole('navigation', { name: 'Table of contents' });
  await expect(toc).toBeVisible();
  await expect(toc.getByRole('button', { name: 'Section 1', exact: true })).toBeVisible();
  await expect(toc.locator('small, [class*="badge"], [class*="card"]')).toHaveCount(0);

  await toc.getByRole('button', { name: 'Section 6', exact: true }).click();
  await expect(deliverable.getByRole('heading', { name: 'Section 6' })).toBeFocused();
  expect(await viewport.evaluate((element) => {
    const node = element as HTMLElement;
    return node.scrollWidth <= node.clientWidth;
  })).toBe(true);
  expect(await page.evaluate(() => document.documentElement.scrollWidth))
    .toBeLessThanOrEqual(1440);
});

test('long-rich-deliverable: contextual TOC navigation reopens and closes on breakpoint change', async ({ page }) => {
  test.setTimeout(120_000);
  await page.setViewportSize({ width: 390, height: 844 });
  await injectScenario('long-rich-deliverable');
  await sendMessage(page, 'scenario:long-rich-deliverable');
  await waitForTurnComplete(page, 90_000);

  const deliverable = page.locator('[data-kind="assistant_deliverable"]').last();
  await deliverable.evaluate((element) => element.scrollIntoView({ block: 'center' }));
  const viewport = page.locator(VIEWPORT);
  await viewport.evaluate(async (element) => {
    await new Promise<void>((resolve) => requestAnimationFrame(() => requestAnimationFrame(() => resolve())));
    const node = element as HTMLElement;
    node.dispatchEvent(new WheelEvent('wheel', { bubbles: true, deltaY: -120 }));
    node.scrollTop = Math.max(0, node.scrollTop - 24);
    node.dispatchEvent(new Event('scroll', { bubbles: true }));
  });
  const tocTrigger = deliverable.getByRole('button', { name: 'Open table of contents' });
  await expect(tocTrigger).toBeVisible();

  await tocTrigger.click();
  await page.getByTestId('rich-deliverable-toc').getByRole('button', { name: 'Section 3' }).click();
  await expect(page.getByRole('dialog', { name: 'Table of contents' })).toHaveCount(0);
  await expect(page.getByTestId('rich-deliverable-toc')).toHaveCount(1);
  await expect(deliverable.getByRole('heading', { name: 'Section 3' })).toBeFocused();

  await expect(tocTrigger).toBeVisible();
  await tocTrigger.click();
  await page.getByTestId('rich-deliverable-toc').getByRole('button', { name: 'Section 6' }).click();
  await expect(page.getByRole('dialog', { name: 'Table of contents' })).toHaveCount(0);
  await expect(page.getByTestId('rich-deliverable-toc')).toHaveCount(1);
  await expect(deliverable.getByRole('heading', { name: 'Section 6' })).toBeFocused();

  await expect(tocTrigger).toBeVisible();
  await tocTrigger.click();
  await expect(page.getByRole('dialog', { name: 'Table of contents' })).toBeVisible();
  await page.setViewportSize({ width: 1024, height: 844 });
  await expect(page.getByTestId('rich-deliverable-toc')).toHaveCount(1);
  await expect(page.getByRole('dialog', { name: 'Table of contents' })).toHaveCount(0);
  await page.setViewportSize({ width: 390, height: 844 });
  await expect(page.getByTestId('rich-deliverable-toc')).toHaveCount(1);
});

test('grouped-navigation: targets the exact long assistant among tools and siblings', async ({ page }) => {
  test.setTimeout(120_000);
  await page.setViewportSize({ width: 390, height: 844 });
  await injectScenario('grouped-navigation');
  await sendMessage(page, 'scenario:grouped-navigation');
  await waitForTurnComplete(page, 90_000);

  const activity = page.locator('[data-kind="activity_segment"]').last();
  await expect(activity).toBeVisible();
  const expand = activity.getByRole('button').first();
  if (await expand.isVisible()) await expand.click();

  const assistants = page.locator(`${VIEWPORT} [data-kind="message"][data-role="assistant"]`);
  await expect(assistants).toHaveCount(3);
  await expect(activity.locator('[data-kind="message"][data-role="assistant"]')).toHaveCount(2);
  const longTarget = assistants.filter({ hasText: 'Long semantic assistant target.' });
  await expect(longTarget).toBeVisible();
  await longTarget.evaluate((element) => element.scrollIntoView({ block: 'center' }));
  await page.mouse.wheel(0, -160);

  const start = page.locator(SCROLL_TO_ACTIVE_START);
  await expect(start).toBeVisible();
  await expect(page.getByTestId('timeline-viewport-navigation-cluster').getByRole('button')).toHaveCount(2);
  await start.click();

  const positions = await page.evaluate(() => {
    const viewport = document.querySelector<HTMLElement>('[data-testid="timeline-viewport"]');
    const assistants = Array.from(document.querySelectorAll<HTMLElement>(
      '[data-kind="message"][data-role="assistant"]',
    ));
    const target = assistants.find((element) => element.textContent?.includes('Long semantic assistant target.'));
    const short = assistants.find((element) => element.textContent?.includes('Short assistant before tools.'));
    if (!viewport || !target || !short) throw new Error('Grouped navigation fixture is incomplete');
    return {
      targetOffset: Math.abs(target.getBoundingClientRect().top - viewport.getBoundingClientRect().top),
      shortOffset: Math.abs(short.getBoundingClientRect().top - viewport.getBoundingClientRect().top),
    };
  });
  expect(positions.targetOffset).toBeLessThan(24);
  expect(positions.shortOffset).toBeGreaterThan(80);
});
