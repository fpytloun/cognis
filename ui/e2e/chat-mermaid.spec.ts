import { expect, test, type Locator } from '@playwright/test';
import { diagram } from './fixtures/chat-markdown';

async function transform(viewer: Locator) {
  return viewer.locator('.diagram-transform').evaluate(el => {
    const matrix = new DOMMatrix(getComputedStyle(el).transform);
    return { scale: matrix.a, x: matrix.e, y: matrix.f };
  });
}

async function controlStyle(button: Locator) {
  return button.evaluate(el => {
    const style = getComputedStyle(el), icon = getComputedStyle(el.querySelector('svg')!);
    return {
      width: el.getBoundingClientRect().width, height: el.getBoundingClientRect().height,
      iconWidth: icon.width, iconHeight: icon.height,
      border: style.border, radius: style.borderRadius, background: style.backgroundColor,
      gap: getComputedStyle(el.parentElement!).gap,
    };
  });
}

for (const family of ['paper', 'classic']) {
  test(`${family}: both message roles render, pan, zoom, copy and open fullscreen`, async ({ page, context }, info) => {
    const errors: string[] = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.setViewportSize({ width: 1440, height: 1100 });
    await context.grantPermissions(['clipboard-read', 'clipboard-write']);
    await page.goto('/chat-deliverable-fixture?markdown&chrome');
    await page.locator('[data-message-role=user]').waitFor();
    await page.evaluate(family => {
      document.documentElement.setAttribute('data-theme-family', family);
      document.documentElement.setAttribute('data-resolved-theme', family === 'paper' ? 'light' : 'dark');
    }, family);
    const assistant = page.locator('[data-message-role=assistant] [data-mermaid-viewer]');
    const user = page.locator('[data-message-role=user] [data-mermaid-viewer]');
    await expect(assistant.locator('svg.flowchart')).toBeVisible();
    await expect(user.locator('svg.flowchart')).toBeVisible();
    await expect(user).toHaveAttribute('aria-busy', 'false');
    await expect(assistant).toHaveAttribute('aria-busy', 'false');
    await expect(user.locator('svg.flowchart')).toContainText('Superset dashboards');
    await expect(user.locator('svg.flowchart')).not.toContainText('&gt;');
    const codeControl = page.locator('[data-message-role=user]').getByRole('button', { name: 'Copy code block' }).first();
    const expectedControl = await controlStyle(codeControl);
    for (const control of await user.getByRole('button').all()) {
      expect(await controlStyle(control)).toEqual(expectedControl);
    }
    const codeExpand = page.locator('[data-message-role=user]').getByRole('button', { name: 'Open code block in modal' }).first();
    const diagramExpand = user.getByRole('button', { name: 'Open diagram fullscreen' });
    expect(await diagramExpand.locator('svg').innerHTML()).toBe(await codeExpand.locator('svg').innerHTML());
    const diagramCopy = user.getByRole('button', { name: 'Copy diagram source' });
    const copyIcon = await codeControl.locator('svg').innerHTML();
    expect(await diagramCopy.locator('svg').innerHTML()).toBe(copyIcon);
    if (family === 'classic') {
      await expect(page.locator('[data-message-role=user] .chat-markdown')).toHaveCSS('color', 'rgb(248, 250, 252)');
      await expect(page.locator('[data-message-role=user] .chat-markdown h2')).toHaveCSS('color', 'rgb(248, 250, 252)');
      await expect(page.locator('[data-message-role=assistant] .chat-markdown')).toHaveCSS('color', 'rgb(203, 213, 225)');
    }
    const ids = await page.locator('[data-mermaid-viewer] svg [id]').evaluateAll(nodes => nodes.map(n => n.id));
    expect(new Set(ids).size).toBe(ids.length);
    await user.scrollIntoViewIfNeeded();
    await user.getByRole('button', { name: 'Fit diagram to box' }).click();
    const fitted = await transform(user);
    await user.getByRole('button', { name: 'Zoom diagram in' }).click();
    expect((await transform(user)).scale).toBeCloseTo(fitted.scale * 1.25, 4);
    await user.getByRole('button', { name: 'Zoom diagram out' }).click();
    expect((await transform(user)).scale).toBeCloseTo(fitted.scale, 4);
    const stage = user.getByRole('application');
    const box = (await stage.boundingBox())!;
    await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
    await page.mouse.down();
    await page.mouse.move(box.x + box.width / 2 + 60, box.y + box.height / 2 + 24, { steps: 8 });
    await page.mouse.up();
    const panned = await transform(user);
    expect(panned.x - fitted.x).toBeCloseTo(60, 1);
    expect(panned.y - fitted.y).toBeCloseTo(24, 1);
    await page.mouse.wheel(0, -200);
    await expect.poll(async () => (await transform(user)).scale).toBeGreaterThan(panned.scale);
    await page.mouse.wheel(0, 200);
    await expect.poll(async () => (await transform(user)).scale).toBeCloseTo(panned.scale, 4);
    await user.getByRole('button', { name: 'Copy diagram source' }).click();
    expect(await page.evaluate(() => navigator.clipboard.readText())).toBe(diagram);
    await expect(user.getByRole('button', { name: 'Diagram copied' })).toBeVisible();
    await expect(user.getByRole('button', { name: 'Copy diagram source' })).toBeVisible();
    expect(await diagramCopy.locator('svg').innerHTML()).toBe(copyIcon);
    await user.getByRole('button', { name: 'Fit diagram to box' }).click();
    await page.screenshot({ path: info.outputPath(`${family}-user-markdown.png`) });
    const opener = user.getByRole('button', { name: 'Open diagram fullscreen' });
    await opener.click();
    const modal = page.getByRole('dialog', { name: 'Mermaid diagram fullscreen' });
    await expect(modal.locator('svg.flowchart')).toBeVisible();
    const modalStage = modal.getByRole('application');
    await modalStage.focus();
    const fullFitted = await transform(modal);
    await page.keyboard.press('+');
    expect((await transform(modal)).scale).toBeGreaterThan(fullFitted.scale);
    await expect(modalStage).toHaveCSS('outline-style', 'none');
    await expect(modalStage).toHaveCSS('box-shadow', 'none');
    const focusedFrame = modal.locator('[data-mermaid-viewer]');
    await expect(focusedFrame).not.toHaveCSS('box-shadow', 'none');
    expect(await controlStyle(modal.getByRole('button', { name: 'Copy diagram source' }))).toEqual(expectedControl);
    await page.screenshot({ path: info.outputPath(`${family}-fullscreen-zoomed.png`) });
    await page.keyboard.press('0');
    expect((await transform(modal)).scale).toBeCloseTo(fullFitted.scale, 4);
    await page.keyboard.press('Escape');
    await expect(modal).not.toBeVisible();
    await expect(opener).toBeFocused();
    await page.getByRole('button', { name: 'Show notification' }).click();
    const toast = page.getByTestId('toast-viewport');
    await expect(toast).toContainText('Conversation created');
    const rect = (await toast.boundingBox())!;
    const viewport = page.viewportSize()!;
    expect(rect.y).toBeLessThan(120);
    expect(rect.x).toBeGreaterThan(viewport.width / 2);
    const composer = (await page.getByTestId('chat-composer-host').boundingBox())!;
    expect(rect.y + rect.height).toBeLessThan(composer.y);
    await page.screenshot({ path: info.outputPath(`${family}-notification.png`) });
    expect(errors).toEqual([]);
  });
}

test('phone: real touch pinch and drag, controls remain reachable without page overflow', async ({ browser }, info) => {
  const context = await browser.newContext({ viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true });
  try {
    const page = await context.newPage();
    await page.goto('/chat-deliverable-fixture?markdown&chrome');
    await page.locator('[data-message-role=user]').waitFor();
    await page.evaluate(() => {
      document.documentElement.setAttribute('data-theme-family', 'paper');
      document.documentElement.setAttribute('data-resolved-theme', 'light');
    });
    const viewer = page.locator('[data-message-role=user] [data-mermaid-viewer]');
    await expect(viewer.locator('svg.flowchart')).toBeVisible();
    await expect(viewer).toHaveAttribute('aria-busy', 'false');
    const codeControl = page.locator('[data-message-role=user]').getByRole('button', { name: 'Copy code block' }).first();
    const expectedControl = await controlStyle(codeControl);
    for (const control of await viewer.getByRole('button').all()) {
      expect(await controlStyle(control)).toEqual(expectedControl);
    }
    await page.locator('[data-message-role=user] h2').scrollIntoViewIfNeeded();
    await page.screenshot({ path: info.outputPath('phone-paper-code.png') });
    await viewer.scrollIntoViewIfNeeded();
    await page.screenshot({ path: info.outputPath('phone-paper-markdown.png') });
    const inlineFit = await transform(viewer);
    await viewer.getByRole('button', { name: 'Zoom diagram in' }).tap();
    await expect.poll(async () => (await transform(viewer)).scale).toBeGreaterThan(inlineFit.scale);
    await viewer.getByRole('button', { name: 'Zoom diagram out' }).tap();
    await expect.poll(async () => (await transform(viewer)).scale).toBeCloseTo(inlineFit.scale, 4);
    await viewer.getByRole('button', { name: 'Open diagram fullscreen' }).tap();
    const modal = page.getByRole('dialog');
    await expect(modal.locator('svg.flowchart')).toBeVisible();
    const fullFit = await transform(modal);
    await modal.getByRole('button', { name: 'Zoom diagram in' }).tap();
    await expect.poll(async () => (await transform(modal)).scale).toBeGreaterThan(fullFit.scale);
    await modal.getByRole('button', { name: 'Fit diagram to box' }).tap();
    await expect.poll(async () => (await transform(modal)).scale).toBeCloseTo(fullFit.scale, 4);
    await page.screenshot({ path: info.outputPath('phone-paper-fullscreen.png') });
    await page.getByRole('button', { name: 'Exit diagram fullscreen' }).tap();
    await expect(modal).not.toBeVisible();
    const stage = viewer.getByRole('application');
    const box = (await stage.boundingBox())!;
    const x = box.x + box.width / 2, y = box.y + box.height / 2;
    const fitted = await transform(viewer);
    const cdp = await context.newCDPSession(page);
    const touch = (type: 'touchStart' | 'touchMove' | 'touchEnd', points: { x: number; y: number; id: number }[]) =>
      cdp.send('Input.dispatchTouchEvent', { type, touchPoints: points });
    await touch('touchStart', [{ x: x - 30, y, id: 1 }, { x: x + 30, y, id: 2 }]);
    for (let step = 1; step <= 6; step++) {
      await touch('touchMove', [{ x: x - 30 - step * 5, y, id: 1 }, { x: x + 30 + step * 5, y, id: 2 }]);
    }
    await touch('touchEnd', []);
    expect((await transform(viewer)).scale).toBeGreaterThan(fitted.scale * 1.8);
    await touch('touchStart', [{ x: x - 60, y, id: 1 }, { x: x + 60, y, id: 2 }]);
    for (let step = 1; step <= 6; step++) {
      await touch('touchMove', [{ x: x - 60 + step * 5, y, id: 1 }, { x: x + 60 - step * 5, y, id: 2 }]);
    }
    await touch('touchEnd', []);
    expect((await transform(viewer)).scale).toBeCloseTo(fitted.scale, 4);
    const zoomed = await transform(viewer);
    await touch('touchStart', [{ x, y, id: 1 }]);
    await touch('touchMove', [{ x: x + 35, y: y + 20, id: 1 }]);
    await touch('touchEnd', []);
    expect((await transform(viewer)).x - zoomed.x).toBeCloseTo(35, 1);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    await page.screenshot({ path: info.outputPath('phone-paper-panned.png') });
  } finally { await context.close(); }
});
