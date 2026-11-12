import { expect, test } from '@playwright/test';
import { usageGeometry } from './usage-geometry';
import { writeFileSync } from 'node:fs';

test.use({ viewport: { width: 400, height: 300 } });

for (const clipping of [true, false]) {
  test(`fixed navigation ${clipping ? 'does not paint over ancestor-clipped text' : 'still fails for actual painted overlap'}`, async ({ page }) => {
    await page.setContent(`<style>
      body { margin:0 } nav { position:fixed; bottom:0; height:50px; width:100%; background:white }
      .scrollport { position:absolute; top:240px; height:10px; width:300px; overflow:${clipping ? 'hidden' : 'visible'} }
      span { position:absolute; top:30px; white-space:nowrap }
      </style><div class="scrollport" data-usage-geometry><span>Painted geometry fixture</span></div><nav>Navigation</nav>`);
    const geometry = await page.evaluate(usageGeometry, '[data-usage-geometry]');
    expect(geometry.rawHits.length).toBeGreaterThan(0);
    expect(geometry.covered === 0).toBe(clipping);
    if (clipping) expect(geometry.clipped).toBeGreaterThan(0);
    else {
      expect(geometry.hitDetails[0]).toMatchObject({
        floating: { tag: 'NAV', position: 'fixed', visibility: 'visible' },
        obstacle: { tag: 'SPAN', position: 'absolute' },
        lineRect: { top: 270 }, paintedLineRect: { top: 270 },
      });
    }
  });
}
test('keeps visible message input inside the overlap contract', async ({ page }) => {
  await page.setContent('<div data-usage-geometry>Visible fixture</div><textarea style="position:fixed;bottom:0;height:40px"></textarea><nav style="position:fixed;bottom:0;width:100%;height:50px">Navigation</nav>');
  expect((await page.evaluate(usageGeometry, '[data-usage-geometry]')).hits).toEqual(['NAV']);
});
test('a fixed message input does not cover itself without overlapping chrome', async ({ page }) => {
  await page.setContent('<div data-usage-geometry>Visible fixture</div><textarea style="position:fixed;bottom:0;height:40px"></textarea>');
  expect((await page.evaluate(usageGeometry, '[data-usage-geometry]')).covered).toBe(0);
});
test('does not clip a fixed message input that escapes an ordinary ancestor scrollport', async ({ page }) => {
  await page.setContent('<div data-usage-geometry>Visible fixture</div><div style="height:10px;overflow:hidden"><textarea style="position:fixed;bottom:0;height:40px"></textarea></div><nav style="position:fixed;bottom:0;width:100%;height:50px">Navigation</nav>');
  expect((await page.evaluate(usageGeometry, '[data-usage-geometry]')).hits).toEqual(['NAV']);
});
test('fixed SPAN chrome is not blanket-exempted from painted text', async ({ page }) => {
  await page.setContent('<div data-usage-geometry style="position:absolute;top:260px">Visible text</div><span style="position:fixed;bottom:0;width:100%;height:50px">Overlay</span>');
  const geometry = await page.evaluate(usageGeometry, '[data-usage-geometry]');
  expect(geometry.hits).toContain('SPAN');
  expect(geometry.hitDetails[0]).toMatchObject({
    floating: { tag: 'SPAN', position: 'fixed' },
    clippingChain: expect.any(Array),
  });
});

for (const open of [false, true]) {
  test(`native ${open ? 'open' : 'closed'} disclosure painting characterization`, async ({ page }, info) => {
    await page.setContent('<div style="position:absolute;top:260px">Visible text</div><details open><summary>Summary</summary><span data-overlay style="position:fixed;bottom:0;left:0;width:100%;height:50px;background:white">Overlay</span></details>');
    await page.locator('[data-overlay]').boundingBox();
    await page.locator('details').evaluate((element, value) => { (element as HTMLDetailsElement).open = value; }, open);
    await page.evaluate(() => new Promise<void>((done) => requestAnimationFrame(() => requestAnimationFrame(() => done()))));
    const native = await page.evaluate(() => {
      const element = document.querySelector('[data-overlay]')!;
      return {
        open: document.querySelector('details')!.open,
        visible: element.checkVisibility(),
        hit: document.elementsFromPoint(20, 275).includes(element),
        rect: element.getBoundingClientRect().toJSON(),
      };
    });
    writeFileSync(info.outputPath('native-disclosure.json'), JSON.stringify(native, null, 2));
    expect(native.visible).toBe(open);
    expect(native.hit).toBe(open);
  });
  test(`${open ? 'open' : 'closed'} non-summary content remains ${open ? 'an actual' : 'an unpainted'} text and floating candidate`, async ({ page }) => {
    await page.setContent('<details open data-usage-geometry><summary>Visible summary</summary><span data-hidden style="position:fixed;bottom:0;left:0;height:40px">Disclosure text</span><textarea style="position:fixed;bottom:0;right:0;height:40px"></textarea></details><nav style="position:fixed;bottom:0;left:0;width:100%;height:50px">Navigation</nav>');
    await page.locator('[data-hidden]').boundingBox();
    await page.locator('details').evaluate((element, value) => { (element as HTMLDetailsElement).open = value; }, open);
    const geometry = await page.evaluate(usageGeometry, '[data-usage-geometry]');
    if (open) {
      expect(geometry.hits).toContain('NAV');
      expect(geometry.excludedDisclosures).toHaveLength(0);
    } else {
      expect(geometry.covered).toBe(0);
      expect(geometry.excludedDisclosures.length).toBeGreaterThan(0);
    }
  });
}

test('the first summary of a closed disclosure remains visible and protected against fixed overlays', async ({ page }) => {
  await page.setContent('<details data-usage-geometry><summary style="position:absolute;top:260px">Visible summary</summary><div>Hidden body</div></details><nav style="position:fixed;bottom:0;width:100%;height:50px">Navigation</nav>');
  expect(await page.locator('summary').evaluate((element) => element.checkVisibility())).toBe(true);
  expect((await page.evaluate(usageGeometry, '[data-usage-geometry]')).hits).toContain('NAV');
});

test('an inner summary is still suppressed by an outer closed disclosure', async ({ page }) => {
  await page.setContent('<div data-usage-geometry><details><summary>Outer summary</summary><details open><summary style="position:fixed;bottom:0">Inner summary</summary></details></details></div><nav style="position:fixed;bottom:0;width:100%;height:50px">Navigation</nav>');
  expect(await page.locator('details details summary').evaluate((element) => element.checkVisibility())).toBe(false);
  expect((await page.evaluate(usageGeometry, '[data-usage-geometry]')).covered).toBe(0);
});
