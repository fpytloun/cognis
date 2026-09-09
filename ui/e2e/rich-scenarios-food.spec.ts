import { expect, test } from '@playwright/test';

const scenarios = ['illustrated-recipe', 'weekly-meal-plan'] as const;

async function expectNoHorizontalClipping(page: import('@playwright/test').Page) {
  const overflow = await page.evaluate(() => {
    const viewportWidth = document.documentElement.clientWidth;
    const offenders = Array.from(document.querySelectorAll<HTMLElement>('body *'))
      .filter((element) => {
        const style = getComputedStyle(element);
        const rect = element.getBoundingClientRect();
        if (
          style.position === 'fixed'
          || style.position === 'absolute'
          || style.display === 'none'
          || rect.width === 0
          || rect.height === 0
        ) return false;

        let effectiveRect = rect;
        let ancestor = element.parentElement;
        while (ancestor) {
          const ancestorStyle = getComputedStyle(ancestor);
          const ancestorRect = ancestor.getBoundingClientRect();
          const locallyScrollable = ['auto', 'scroll'].includes(ancestorStyle.overflowX)
            && ancestor.scrollWidth > ancestor.clientWidth + 1;
          const clippedByAncestor = ['hidden', 'clip'].includes(ancestorStyle.overflowX)
            && (effectiveRect.left < ancestorRect.left - 1 || effectiveRect.right > ancestorRect.right + 1);
          if (clippedByAncestor) return true;
          const accessibleScrollport = ancestorRect.left >= -1 && ancestorRect.right <= viewportWidth + 1;
          if (locallyScrollable && accessibleScrollport) effectiveRect = ancestorRect;
          ancestor = ancestor.parentElement;
        }
        return effectiveRect.right > viewportWidth + 1 || effectiveRect.left < -1;
      })
      .slice(0, 12)
      .map((element) => ({
        tag: element.tagName,
        className: element.className,
        left: element.getBoundingClientRect().left,
        right: element.getBoundingClientRect().right,
      }));
    return { documentWidth: document.documentElement.scrollWidth, viewportWidth, offenders };
  });
  expect(overflow.documentWidth, JSON.stringify(overflow.offenders, null, 2))
    .toBeLessThanOrEqual(overflow.viewportWidth + 1);
  expect(overflow.offenders, JSON.stringify(overflow.offenders, null, 2)).toEqual([]);
}

async function loadAllImages(page: import('@playwright/test').Page) {
  await page.locator('img').evaluateAll((images) => {
    for (const image of images) (image as HTMLImageElement).loading = 'eager';
  });
}

for (const scenario of scenarios) {
  test(`${scenario} keeps its food visuals and safety content associated at desktop and mobile widths`, async ({ page }) => {
    for (const width of [390, 1440]) {
      await page.setViewportSize({ width, height: 1200 });
      await page.goto(`/rich-deliverable-fixture?scenario=${scenario}&theme=light&width=${width}&surface=embedded`);

      const fixture = page.getByTestId('rich-deliverable-fixture');
      await expect(fixture).toHaveAttribute('data-scenario', scenario);
      await loadAllImages(page);
      const images = fixture.locator('img');
      expect(await images.count()).toBeGreaterThan(0);
      for (let index = 0; index < await images.count(); index += 1) {
        const image = images.nth(index);
        await expect(image).toHaveJSProperty('complete', true);
        expect(await image.evaluate((element: HTMLImageElement) => element.naturalWidth))
          .toBeGreaterThan(0);
        await expect(image).toHaveAttribute('alt', /\S+/);
      }
      await expect(fixture.getByText('Food safety', { exact: true })).toBeVisible();
      await expectNoHorizontalClipping(page);
    }
  });
}

test('illustrated recipe keeps each numbered step group immediately before its figure', async ({ page }) => {
  await page.goto('/rich-deliverable-fixture?scenario=illustrated-recipe&width=1440&surface=embedded');
  const steps = page.locator('[data-rich-block-type="steps"]');
  await expect(steps).toHaveCount(2);
  await expect(steps.first().locator('ol > li')).toHaveCount(2);
  await expect(steps.nth(1).locator('ol > li')).toHaveCount(2);
  for (let index = 0; index < await steps.count(); index += 1) {
    expect(await steps.nth(index).evaluate((element) =>
      element.parentElement?.nextElementSibling
        ?.querySelector('[data-rich-block-type]')?.getAttribute('data-rich-block-type')
    )).toBe('figure');
  }
  const figures = page.locator('[data-rich-block-type="figure"]');
  await expect(figures).toHaveCount(3);
  await expect(figures.nth(0).locator('img')).toHaveAttribute('src', /mix-and-rise\.svg$/);
  await expect(figures.nth(1).locator('img')).toHaveAttribute('src', /dimple-and-bake\.svg$/);
});

test('weekly meal plan shows grouped shopping checklist markers and recipe-owned images', async ({ page }) => {
  await page.goto('/rich-deliverable-fixture?scenario=weekly-meal-plan&width=1440&surface=embedded');
  await expect(page.getByText('Shopping checklist')).toBeVisible();
  await expect(page.locator('[data-rich-block-type="checklist"] input[type="checkbox"]')).toHaveCount(6);
  const cards = page.locator('[data-rich-card-variant="visual"]');
  await expect(cards).toHaveCount(3);
  for (const [index, filename] of ['traybake.svg', 'lentil-pasta.svg', 'tacos.svg'].entries()) {
    const image = cards.nth(index).locator('img');
    await expect(image).toHaveCount(1);
    await expect(image).toHaveAttribute('src', new RegExp(`${filename.replace('.', '\\.')}$`));
  }
  await expect(page.getByText('Keep raw fish below ready-to-eat food')).toBeVisible();
});
