import { expect, test } from '@playwright/test';

test('loads, updates, and restores scenarios by stable ID', async ({ page }) => {
  await page.goto('/rich-deliverable-fixture?scenario=incident-report&theme=dark&width=768&surface=embedded');
  const fixture = page.getByTestId('rich-deliverable-fixture');
  await expect(fixture).toHaveAttribute('data-scenario', 'incident-report');
  await expect(fixture).toHaveAttribute('data-theme', 'dark');
  await expect(fixture).toHaveAttribute('data-width', '768');

  await page.getByLabel('Scenario', { exact: true }).selectOption('product-comparison');
  await expect(fixture).toHaveAttribute('data-scenario', 'product-comparison');
  await expect(page).toHaveURL(/scenario=product-comparison/);

  await page.goBack();
  await expect(fixture).toHaveAttribute('data-scenario', 'incident-report');
});

test('falls back to the default for an unknown scenario ID', async ({ page }) => {
  await page.goto('/rich-deliverable-fixture?scenario=missing-scenario');
  await expect(page.getByTestId('rich-deliverable-fixture')).toHaveAttribute(
    'data-scenario',
    'research-answer',
  );
});

test('compact toolbar preserves query controls and stays outside captures', async ({ page }) => {
  await page.setViewportSize({ width: 1230, height: 1133 });
  await page.goto('/rich-deliverable-fixture?scenario=claude-viewer-parity&surface=embedded&theme=light');
  const toolbar = page.getByTestId('rich-gallery-toolbar');
  expect((await toolbar.boundingBox())!.height).toBe(56);
  await page.getByLabel('Preview width').selectOption('1230');
  await page.getByLabel('Preview surface').selectOption('standalone');
  await page.getByLabel('Preview theme').selectOption('dark');
  await expect(page.getByTestId('rich-deliverable')).toHaveAttribute('data-viewer-theme', 'dark');
  await expect(page).toHaveURL(/width=1230/);
  await page.evaluate(() => document.fonts.ready);
  await page.screenshot({ path: '/tmp/rich-gallery-compact.png' });
  await page.goto('/rich-deliverable-fixture?scenario=claude-viewer-parity&capture=viewer');
  await expect(toolbar).toHaveCount(0);
});

test('standalone query theme overrides saved preference on direct load and history', async ({ page }) => {
  await page.addInitScript(() => localStorage.setItem('cognis-rich-viewer-theme', 'dark'));
  await page.goto('/rich-deliverable-fixture?theme=light&surface=standalone');
  const viewer = page.getByTestId('rich-deliverable');
  await expect(viewer).toHaveAttribute('data-viewer-theme', 'light');
  await page.getByLabel('Preview theme').selectOption('dark');
  await expect(viewer).toHaveAttribute('data-viewer-theme', 'dark');
  await page.goBack();
  await expect(viewer).toHaveAttribute('data-viewer-theme', 'light');
  await page.goForward();
  await expect(viewer).toHaveAttribute('data-viewer-theme', 'dark');
});

test('restores the document theme after leaving the fixture', async ({ page }) => {
  await page.emulateMedia({ colorScheme: 'light' });
  await page.goto('/rich-deliverable-fixture?theme=light');
  await expect.poll(() => page.evaluate(() => document.documentElement.dataset.resolvedTheme)).toBe('light');
  await expect.poll(() => page.evaluate(() => document.documentElement.style.colorScheme)).toBe('light');
  const appearance = () => page.evaluate(() => ({
    family: document.documentElement.dataset.themeFamily,
    theme: document.documentElement.dataset.resolvedTheme,
    dark: document.documentElement.classList.contains('dark'),
    colorScheme: document.documentElement.style.colorScheme,
    chrome: document.querySelector('meta[name="theme-color"]')?.getAttribute('content'),
  }));
  const original = await appearance();
  await page.evaluate(() => {
    (window as Window & { fixtureNavigationDocument?: boolean }).fixtureNavigationDocument = true;
  });
  const navigate = async (url: string) => {
    await page.evaluate(href => {
      const link = document.createElement('a');
      link.href = href;
      link.textContent = 'Navigate fixture';
      link.dataset.testid = 'fixture-navigation';
      link.style.cssText = 'position:fixed;top:0;left:0;z-index:9999';
      document.body.append(link);
    }, url);
    await page.getByTestId('fixture-navigation').click();
    await page.getByTestId('fixture-navigation').evaluate(node => node.remove());
  };
  await navigate('/rich-deliverable-fixture?theme=dark');
  await expect.poll(() => page.evaluate(() => document.documentElement.dataset.resolvedTheme)).toBe('dark');
  await navigate('/rich-deliverable-block-fixture?block=hero');
  await expect.poll(appearance).toEqual(original);
  expect(await page.evaluate(() =>
    (window as Window & { fixtureNavigationDocument?: boolean }).fixtureNavigationDocument)).toBe(true);
});

test('loads every gallery scenario without unsupported blocks', async ({ page }) => {
  await page.goto('/rich-deliverable-fixture');
  const tabs = page.locator('[data-scenario-id]');
  const ids = await tabs.evaluateAll((elements) => elements.map((element) => element.getAttribute('data-scenario-id')));
  for (const id of ids) {
    await page.goto(`/rich-deliverable-fixture?scenario=${encodeURIComponent(id ?? '')}`);
    await expect(page.getByTestId('rich-deliverable-fixture')).toHaveAttribute('data-scenario', id ?? '');
    await expect(page.getByText(/Unsupported block:/)).toHaveCount(0);
  }
});
