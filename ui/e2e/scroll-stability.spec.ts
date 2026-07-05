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

test('long-streaming-response: manual scroll-up is preserved during streaming', async ({
  page,
}) => {
  test.setTimeout(120_000);
  await injectScenario('long-streaming-response');
  await sendMessage(page, 'scenario:long-streaming-response');

  const viewport = page.locator(VIEWPORT);
  await expect(viewport).toBeVisible();

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

  await waitForTurnComplete(page, 90_000);
});
