import { test, expect } from '@playwright/test';
import { COMPONENT_RUNTIME_ORIGIN } from './component-runtime';

test.use({ serviceWorkers: 'block' });
for (const move of [false, true]) {
  test(`held history preserves the row anchor with in-flight movement=${move}`, async ({ page }) => {
    await page.goto(`${COMPONENT_RUNTIME_ORIGIN}/scoped.html`);
    const viewport = page.getByTestId('scoped-timeline-shell').getByTestId('scoped-timeline-viewport');
    await expect(page.getByTestId('scoped-timeline-shell').getByRole('heading', { name: 'Parent conversation event 20' })).toBeVisible();
    await expect.poll(() => viewport.evaluate(e => e.scrollHeight-e.scrollTop-e.clientHeight)).toBeLessThanOrEqual(2);
    await page.evaluate(() => {
      const w = window as any;
      const v = document.querySelector<HTMLElement>('[data-testid="scoped-timeline-viewport"]')!;
      let key = '';
      const records: unknown[] = [];
      const sample = (phase: string, event?: Event) => {
        const row = [...v.querySelectorAll<HTMLElement>('[data-timeline-row-key]')]
          .find(e => e.dataset.timelineRowKey === key);
        records.push({phase,time:performance.now(),height:v.scrollHeight,top:v.scrollTop,key,
          y:row ? row.getBoundingClientRect().top-v.getBoundingClientRect().top : null,
          trusted:event?.isTrusted});
      };
      const api = w.__scopedFixtureController.api;
      const original = api.timeline;
      w.__historyRequests = 0;
      api.timeline = async (...args: unknown[]) => {
        w.__historyRequests++;
        const row = [...v.querySelectorAll<HTMLElement>('[data-timeline-row-key]')]
          .find(e => e.getBoundingClientRect().bottom > v.getBoundingClientRect().top);
        key = row!.dataset.timelineRowKey!;
        sample('request-entry');
        const response = await original(...args);
        await new Promise<void>(resolve => {w.__releaseHistory = resolve;});
        sample('response-release');
        return response;
      };
      for(const type of ['wheel','scroll']) v.addEventListener(type,e=>sample(type,e));
      new ResizeObserver(()=>sample('resize')).observe(v.firstElementChild!);
      w.__historyRecords = records;
      w.__historySample = sample;
    });
    await viewport.hover();
    await viewport.evaluate(e => {e.scrollTop=100;});
    await page.mouse.wheel(0,-500);
    await page.waitForFunction(() => Boolean((window as any).__releaseHistory));
    if(move) await page.mouse.wheel(0,80);
    await page.evaluate(async () => {
      await new Promise(requestAnimationFrame);
      await new Promise(requestAnimationFrame);
      (window as any).__historySample('before-release');
      (window as any).__releaseHistory();
      await Promise.resolve();
      (window as any).__historySample('microtask');
      for(let i=0;i<4;i++){
        await new Promise(requestAnimationFrame);
        (window as any).__historySample(`raf-${i}`);
      }
    });
    const records = await page.evaluate(()=>(window as any).__historyRecords);
    const before = records.find((r: any) => r.phase === 'before-release');
    const after = records.find((r: any) => r.phase === 'raf-3');
    expect(before.key).not.toBe('');
    expect(after.key).toBe(before.key);
    expect(Math.abs(after.y - before.y)).toBeLessThanOrEqual(2);
    expect(after.height).toBeGreaterThan(before.height);
    expect(await page.evaluate(() => (window as any).__historyRequests)).toBe(1);
  });
}
