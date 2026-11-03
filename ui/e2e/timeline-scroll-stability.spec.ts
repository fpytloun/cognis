import { test, expect } from '@playwright/test';
import { COMPONENT_RUNTIME_ORIGIN } from './component-runtime';

// Regression: scrolling up through chat history must move the content being
// read by exactly the scroll distance. Rows rendered with placeholder heights
// (content-visibility) resolved to their real size while scrolling up and
// shifted the visible content on iOS Safari.
test.use({ serviceWorkers: 'block' });

test('scrolling up moves the visible row exactly by the scroll distance', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 700 });
  await page.goto(`${COMPONENT_RUNTIME_ORIGIN}/scoped.html`);
  const shell = page.getByTestId('scoped-timeline-shell');
  const viewport = shell.getByTestId('scoped-timeline-viewport');
  await expect(shell.getByRole('heading', { name: 'Parent conversation event 20' })).toBeVisible();
  await expect.poll(() => viewport.evaluate((e) => e.scrollHeight - e.scrollTop - e.clientHeight)).toBeLessThanOrEqual(2);

  const result = await viewport.evaluate(async (v) => {
    const frame = () => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve)));
    const rows = () => [...v.querySelectorAll<HTMLElement>('[data-timeline-row-key]')];
    const deferred = rows().filter((row) => getComputedStyle(row).contentVisibility === 'auto').length;
    const errors: number[] = [];
    for (let step = 0; step < 40 && v.scrollTop > 160; step += 1) {
      const top = v.getBoundingClientRect().top;
      const row = rows().find((element) => element.getBoundingClientRect().bottom > top + 4);
      if (!row) break;
      const firstKeyBefore = rows()[0]?.dataset.timelineRowKey;
      const before = row.getBoundingClientRect().top;
      v.scrollBy(0, -120);
      await frame();
      // Skip only steps where older history was prepended (covered by
      // scoped-history-anchor.spec.ts); any other height change, such as a
      // row resolving its real size, must not move the visible content.
      if (rows()[0]?.dataset.timelineRowKey !== firstKeyBefore) continue;
      errors.push(Math.abs(row.getBoundingClientRect().top - before - 120));
    }
    return { deferred, steps: errors.length, worst: Math.max(0, ...errors) };
  });

  expect(result.deferred).toBe(0);
  expect(result.steps).toBeGreaterThan(3);
  expect(result.worst).toBeLessThanOrEqual(1);
});
