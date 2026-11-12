import { test, expect } from '@playwright/test';
import { htmlBridgeScript } from '../src/lib/feedback/annotations/html-bridge';

// Browser-unit qualification only; integrated API-backed acceptance is run by the parent.
test('opaque HTML selection preserves View behavior and never gains parent DOM access', async ({ page }) => {
  const script = htmlBridgeScript('browser-selection-instance');
  await page.setContent('<main id="private-chrome">Private application state</main><iframe title="Untrusted artifact" sandbox="allow-scripts"></iframe>');
  await page.evaluate(html => {
    const frame = document.querySelector('iframe')!;
    const observations: unknown[] = [];
    Object.assign(window, { annotationObservations: observations });
    window.addEventListener('message', event => { if (event.source === frame.contentWindow) observations.push(event.data); });
    frame.srcdoc = `${html}<button id="action" onclick="this.textContent='Action performed'">Run action</button>
      <script>try { parent.document.querySelector('#private-chrome').textContent='Escaped'; } catch {}</script>`;
  }, script);
  const frame = page.frameLocator('iframe');
  await frame.getByRole('button').click();
  await expect(frame.getByRole('button')).toHaveText('Action performed');
  await expect(page.locator('#private-chrome')).toHaveText('Private application state');
  await page.evaluate(() => document.querySelector('iframe')!.contentWindow!.postMessage({ type: 'cognis-mode', instance: 'browser-selection-instance', enabled: true }, '*'));
  await frame.getByRole('button').click();
  await expect.poll(() => page.evaluate(() => (window as unknown as { annotationObservations: { type: string; selector?: string }[] }).annotationObservations.filter(value => value.type === 'cognis-selection' && value.selector).length)).toBe(1);
  expect(await page.locator('iframe').getAttribute('sandbox')).toBe('allow-scripts');
});
