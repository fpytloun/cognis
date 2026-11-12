import { expect, test } from '@playwright/test';
import { installTaskCockpitFixture } from './task-cockpit-fixture';

test.use({
  serviceWorkers: 'block',
  bypassCSP: process.env.COGNIS_E2E_STATIC_PREVIEW === '1',
});

for (const viewport of [{ width: 1280, height: 900 }, { width: 390, height: 844 }]) {
  test(`session and theme recover without navigation or reload at ${viewport.width}px`, async ({ page }) => {
    await page.setViewportSize(viewport);
    const navigation: string[] = [];
    page.on('framenavigated', frame => {
      if (frame === page.mainFrame()) navigation.push(new URL(frame.url()).pathname);
    });
    const state = await installTaskCockpitFixture(page);
    const user = { email: 'owner@cognis.test', role: 'user', name: 'Owner' };
    let meCalls = 0, refreshCalls = 0, preferenceCalls = 0;
    let releasePreferences!: () => void;
    const preferencesReady = new Promise<void>(resolve => { releasePreferences = resolve; });
    await page.route('**/api/auth/me', route => {
      meCalls++;
      return meCalls === 1 ? route.fulfill({ status: 401, json: {} }) : route.fulfill({ json: user });
    });
    await page.route('**/api/auth/refresh', route => {
      refreshCalls++;
      return route.fulfill({ json: { status: 'authenticated', user, expires_at: '2099-01-01T00:00:00Z' } });
    });
    await page.route('**/api/v1/user-preferences', async route => {
      if (route.request().method() !== 'GET') return route.fallback();
      preferenceCalls++;
      await preferencesReady;
      return route.fallback();
    });
    await page.route(/\/api\/v1\/inbox(?:\?.*)?$/, route => route.fulfill({
      json: { items: [], unread_count: 0, next_cursor: null },
    }));
    await page.route('**/api/v1/dashboard/navigation-summary', route => route.fulfill({ json: {
      chat_groups: [], dashboard: { total: 0, critical: 0, warning: 0, info: 0, truncated: false },
      schedules: 0, tasks: 0,
    } }));
    await page.route('**/api/v1/feedback/conversations/conv-task-chat/staging', route => route.fulfill({
      json: { conversation_id: 'conv-task-chat', version: 1, items: [], receipts: [], subject_ids: [] },
    }));
    await page.routeWebSocket('**/api/ws', ws => {
      ws.send(JSON.stringify({ type: 'authenticated' }));
      ws.onMessage(message => {
        if (JSON.parse(String(message)).type === 'ping') ws.send(JSON.stringify({ type: 'pong' }));
      });
    });
    await page.addInitScript(() => {
      const palette = { family: 'paper', chrome: '#1c1b1a', bg: '28 27 26', surface: '36 35 33',
        text: '222 217 207', border: '70 66 59', accent: '207 133 91', onAccent: '28 27 26' };
      localStorage.setItem('cognis-startup-appearance', JSON.stringify({
        v: 1, theme: 'dark', light: palette, dark: palette,
      }));
    });
    await page.goto('/chat/conv-task-chat');
    await expect.poll(() => preferenceCalls).toBeGreaterThan(0);
    expect(refreshCalls).toBe(1);
    await expect(page.locator('html')).toHaveAttribute('data-theme-family', 'paper');
    await expect(page.locator('html')).toHaveAttribute('data-resolved-theme', 'dark');
    expect(await page.evaluate(() => JSON.parse(localStorage.getItem('cognis-startup-appearance')!).dark.family)).toBe('paper');
    await page.screenshot({ path: test.info().outputPath('pending-preferences.png') });
    releasePreferences();
    await expect(page.locator('html')).toHaveAttribute('data-theme-family', 'classic');
    await expect(page.locator('html')).toHaveAttribute('data-resolved-theme', 'dark');
    await expect(page.getByRole('textbox', { name: 'Chat message', exact: true })).toBeEditable();
    const documentStarted = await page.evaluate(() => performance.timeOrigin);
    await page.evaluate(() => {
      const original = IDBFactory.prototype.open;
      Reflect.set(window, 'coordinationFaults', 0);
      IDBFactory.prototype.open = function (name, version) {
        if (name === 'cognis-profile-intent-v1') {
          IDBFactory.prototype.open = original;
          Reflect.set(window, 'coordinationFaults', 1);
          throw new DOMException('Simulated WebKit connection loss', 'InvalidStateError');
        }
        return original.call(this, name, version);
      };
      window.dispatchEvent(new Event('pageshow'));
    });
    await expect.poll(() => page.evaluate(() => Reflect.get(window, 'coordinationFaults'))).toBe(1);
    await expect.poll(() => meCalls).toBe(2);
    await expect(page.getByRole('textbox', { name: 'Chat message', exact: true })).toBeEditable();
    await expect(page).toHaveURL(/\/chat\/conv-task-chat$/);
    await expect(page.locator('html')).toHaveAttribute('data-theme-family', 'classic');
    await expect(page.locator('html')).toHaveAttribute('data-resolved-theme', 'dark');
    expect(await page.evaluate(() => performance.timeOrigin)).toBe(documentStarted);
    expect(navigation.some(path => path === '/login')).toBe(false);
    expect(refreshCalls).toBe(1);
    await page.screenshot({ path: test.info().outputPath('recovered-session.png') });
    expect(state.unmockedRequests()).toEqual([]);
  });
}
