import { expect, test } from '@playwright/test';
import { installTaskCockpitFixture } from './task-cockpit-fixture';

// Static preview cannot stamp the controller's SPA bootstrap nonce. This opt-in
// is fixture-only, not deployment CSP qualification.
test.use({
  serviceWorkers: 'block',
  bypassCSP: process.env.COGNIS_E2E_STATIC_PREVIEW === '1',
});

test('ordinary messages and composer start without opening optional views', async ({ page }, testInfo) => {
  await installTaskCockpitFixture(page);
  await page.route('**/api/auth/me', route => route.fulfill({
    json: { email: 'owner@cognis.test', role: 'user', name: 'Owner' },
  }));
  await page.routeWebSocket('**/api/ws', ws => {
    ws.send(JSON.stringify({ type: 'authenticated' }));
    ws.onMessage(message => {
      if (JSON.parse(String(message)).type === 'ping') ws.send(JSON.stringify({ type: 'pong' }));
    });
  });
  const cdp = await page.context().newCDPSession(page);
  await cdp.send('Network.enable');
  await cdp.send('Network.setCacheDisabled', { cacheDisabled: true });
  await cdp.send('Performance.enable');
  await cdp.send('Emulation.setCPUThrottlingRate', { rate: 4 });
  await page.addInitScript(() => {
    const state = window as typeof window & { ordinaryChatPaint?: number };
    let scheduled = false;
    const observer = new MutationObserver(() => {
      if (scheduled || state.ordinaryChatPaint !== undefined) return;
      const input = document.querySelector<HTMLTextAreaElement>('textarea');
      if (!input || input.disabled || !document.querySelector('[data-message-role="assistant"]')) return;
      scheduled = true;
      requestAnimationFrame(() => {
        const visible = [...document.querySelectorAll<HTMLElement>('[data-message-role="assistant"]')]
          .some(node => {
            const rect = node.getBoundingClientRect();
            return Boolean(node.textContent?.trim()) && rect.height > 0 && rect.bottom > 0 && rect.top < innerHeight;
          });
        if (visible && input.isConnected && !input.disabled) {
          state.ordinaryChatPaint = performance.now();
          observer.disconnect();
        } else scheduled = false;
      });
    });
    observer.observe(document, { childList: true, subtree: true, attributes: true });
  });
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto('/chat/conv-task-chat');
  const composer = page.locator('textarea').first();
  await expect(composer).toBeEditable();
  await expect(page.getByText(/Release evidence answer 6:/)).toBeVisible();
  await expect.poll(() => page.evaluate(() =>
    (window as typeof window & { ordinaryChatPaint?: number }).ordinaryChatPaint
  )).toBeGreaterThan(0);
  await expect(page.getByTestId('rich-deliverable')).toHaveCount(0);

  const browserMetrics = await cdp.send('Performance.getMetrics');
  const metrics = {
    ...await page.evaluate(() => {
      const resources = performance.getEntriesByType('resource') as PerformanceResourceTiming[];
      return {
        firstOrdinaryFrameMs: (window as typeof window & { ordinaryChatPaint?: number }).ordinaryChatPaint,
        jsEncodedBytes: resources.filter(item => /\.js(?:\?|$)/.test(item.name))
          .reduce((sum, item) => sum + item.encodedBodySize, 0),
        cssEncodedBytes: resources.filter(item => /\.css(?:\?|$)/.test(item.name))
          .reduce((sum, item) => sum + item.encodedBodySize, 0),
      };
    }),
    ...Object.fromEntries(browserMetrics.metrics.filter(item =>
      ['ScriptDuration', 'LayoutDuration', 'RecalcStyleDuration', 'TaskDuration'].includes(item.name)
    ).map(item => [item.name, item.value])),
  };
  console.log(`ordinary-chat-startup ${JSON.stringify(metrics)}`);
  await testInfo.attach('ordinary-chat-startup', {
    body: JSON.stringify(metrics, null, 2), contentType: 'application/json',
  });
  await page.route(/\.js(?:\?|$)/, route => route.abort());
  await composer.fill('Ordinary chat stays responsive');
  await expect(composer).toHaveValue('Ordinary chat stays responsive');
});
