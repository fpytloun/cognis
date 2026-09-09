export function assertImageLoaded(image) {
  if (!image.complete || image.naturalWidth <= 0) {
    throw new Error(`Rich fixture image did not load: ${image.src ?? '<missing src>'}`);
  }
}

export async function waitForChartsReady(
  readStates,
  { attempts = 50, intervalMs = 50, sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms)) } = {},
) {
  for (let attempt = 0; attempt < attempts; attempt += 1) {
    const states = await readStates();
    if (states.every((state) => state === 'true')) return;
    await sleep(intervalMs);
  }
  const states = await readStates();
  const pending = states.filter((state) => state !== 'true').length;
  throw new Error(`Rich fixture charts did not become ready: ${pending} pending`);
}

async function waitForImages(page, { visibleOnly, timeoutMs }) {
  const images = page.locator('img');
  for (let index = 0; index < await images.count(); index += 1) {
    const image = images.nth(index);
    if (visibleOnly && !await image.evaluate((element) => {
      const rect = element.getBoundingClientRect();
      return rect.bottom >= 0 && rect.top <= window.innerHeight;
    })) continue;
    await image.evaluate(async (element, timeout) => {
      if (element.complete) return;
      await Promise.race([
        new Promise((resolve) => {
          element.addEventListener('load', resolve, { once: true });
          element.addEventListener('error', resolve, { once: true });
        }),
        new Promise((resolve) => setTimeout(resolve, timeout)),
      ]);
    }, timeoutMs);
    const state = await image.evaluate((element) => ({
      complete: element.complete,
      naturalWidth: element.naturalWidth,
      src: element.currentSrc || element.src,
    }));
    assertImageLoaded(state);
  }
}

export async function waitForRichFixture(page) {
  await page.locator('[data-testid="rich-deliverable"]').waitFor({ state: 'visible' });
  await page.evaluate(async () => document.fonts?.ready);
  await waitForImages(page, { visibleOnly: true, timeoutMs: 2_000 });
  await waitForChartsReady(() =>
    page.locator('.rich-chart-canvas').evaluateAll((charts) =>
      charts.map((chart) => chart.getAttribute('data-chart-ready'))
    )
  );
  await page.waitForTimeout(160);
}

export async function waitForFullPageRichFixture(page) {
  await waitForRichFixture(page);
  await page.evaluate(async () => {
    // The fixture app owns a non-scrollable viewport (`body` is clipped), so
    // window scrolling cannot activate native lazy loading for below-fold
    // media. Make fixture media eager before the full-page readiness check.
    for (const image of document.querySelectorAll('img')) image.loading = 'eager';
    await document.fonts?.ready;
  });
  await waitForImages(page, { visibleOnly: false, timeoutMs: 5_000 });
}
