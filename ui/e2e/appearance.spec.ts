import { expect, test, type Locator, type Page } from '@playwright/test';
import { DEFAULT_USER_PREFERENCES } from '../src/lib/user-preferences';
import { installTaskCockpitFixture, TASK_ID } from './task-cockpit-fixture';
import { THEME_FAMILIES } from '../src/lib/theme/catalog';
import { mkdirSync } from 'node:fs';
for (const mode of ['light', 'dark']) {
  test(`Paper ${mode} actual mobile conversation sidebar scrim`, async ({ page }) => {
    await installAppearanceFixture(page);
    await page.setViewportSize({ width: 390, height: 844 });
    await page.emulateMedia({ reducedMotion: 'reduce' });
    await page.goto('/settings?tab=display');
    await page.getByRole('combobox', { name: /family/i }).selectOption('paper');
    await page.getByRole('combobox', { name: /^Appearance\b/ }).selectOption(mode);
    await page.goto('/chat/conv-task-chat');
    await page.getByRole('button', { name: 'Open conversations', exact: true }).click();
    const overlay = page.getByRole('button', { name: 'Close conversation list', exact: true });
    await expect(overlay).toBeVisible();
    await expect(overlay).toHaveCSS('background-color', mode === 'dark' ? 'rgba(20, 19, 18, 0.8)' : 'rgba(32, 32, 31, 0.8)');
    await expect(page.getByRole('dialog', { name: 'Conversation list' })).toBeVisible();
    mkdirSync('/tmp/cognis-neutral-review/candidate', { recursive: true });
    await page.screenshot({ path: `/tmp/cognis-neutral-review/candidate/paper-${mode}-actual-sidebar-390.png` });
  });
}

test.use({ serviceWorkers: 'block' });
test('local review actual dashboard and floating window capture', async ({ page }) => {
  test.setTimeout(240_000);
  const phase = process.env.COGNIS_REVIEW_PHASE;
  test.skip(!phase, 'Explicit local review capture only');
  const directory = `/tmp/cognis-neutral-review/${phase}`;
  mkdirSync(directory, { recursive: true });
  await page.emulateMedia({ colorScheme: 'light', reducedMotion: 'reduce' });
  const fixture = await installAppearanceFixture(page, true);
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.goto('/settings?tab=display');
  const familySelect = page.getByRole('combobox', { name: /family/i });
  const modeSelect = page.getByRole('combobox', { name: /^Appearance\b/ });
  await expect(familySelect).toBeVisible();
  for (const family of ['porcelain', 'classic', 'paper', 'gruvbox']) {
    await familySelect.selectOption(family);
    for (const mode of ['light', 'dark']) {
      await modeSelect.selectOption(mode);
      await expect(page.locator('html')).toHaveAttribute('data-resolved-theme', mode);
      await page.goto('/');
      await expect(page.getByRole('textbox', { name: 'Search tasks and conversations' })).toBeVisible();
      await expect(page.getByTestId(`dashboard-task-row-${TASK_ID}`)).toBeVisible();
      await page.mouse.move(0, 0);
      await page.screenshot({ path: `${directory}/${family}-${mode}-dashboard-1440.png` });
      await page.getByTestId(`dashboard-task-row-${TASK_ID}`).click();
      await expect(page.getByRole('dialog').last()).toBeVisible();
      await page.screenshot({ path: `${directory}/${family}-${mode}-floating-window-1440.png` });
      await page.getByRole('dialog').last().getByRole('button', { name: 'Close', exact: true }).click();
      await expect(page.getByRole('dialog')).toHaveCount(0);
      await page.goto('/settings?tab=display');
      await expect(familySelect).toHaveValue(family);
    }
  }
  expect(fixture.pageErrors).toEqual([]);
});
test.setTimeout(120_000);
async function installAppearanceFixture(page: Page, workspaceWindows = false) {
  const pageErrors: string[] = [];
  page.on('pageerror', (error) => {
    pageErrors.push(error.message);
    console.error(`Appearance page error: ${error.message}`);
  });
  // Install a deny-by-default API boundary before the existing product fixture.
  // No authentication or preference write reaches a real backend.
  await page.route(/^https?:\/\/[^/]+\/api\//, (route) => route.fulfill({
    status: 404, json: { detail: 'Outside appearance fixture' },
  }));
  await installTaskCockpitFixture(page, { dashboardWorkspaceWindows: workspaceWindows });
  await page.route('**/api/auth/me', (route) => route.fulfill({
    json: { email: 'owner@cognis.test', name: 'Appearance owner', role: 'admin' },
  }));
  await page.route('**/api/bootstrap-status', (route) => route.fulfill({
    json: { has_users: true, needs_bootstrap: false },
  }));
  await page.route('**/api/health**', (route) => route.fulfill({
    json: { status: 'ok', services: {}, providers: {}, llm_available: true },
  }));
  await page.route('**/api/v1/provider-monitor**', (route) => route.fulfill({ json: [] }));
  await page.route('**/api/v1/schedules**', (route) => route.fulfill({ json: [] }));
  await page.route('**/api/v1/inbox**', (route) => route.fulfill({
    json: { items: [], unread_count: 0, has_more: false, next_cursor: null },
  }));
  await page.route('**/api/v1/dashboard/issues', (route) => route.fulfill({
    json: { issues: [], summary: { total: 0, critical: 0, warning: 0, info: 0 } },
  }));
  await page.route('**/api/v1/dashboard/navigation-summary', (route) => route.fulfill({
    json: { chat_groups: [], dashboard: { total: 0, critical: 0, warning: 0, info: 0, truncated: false }, tasks: 0, schedules: 0 },
  }));
  let preferences = structuredClone(DEFAULT_USER_PREFERENCES);
  preferences.display.language = 'en';
  preferences.display.dashboard_workspace_windows = workspaceWindows;
  const saves: string[] = [];
  const familySaves: string[] = [];
  const bindPreferences = async (target: Page) => {
    await target.route('**/api/v1/user-preferences', async (route) => {
      if (route.request().method() === 'PUT') {
        preferences = route.request().postDataJSON();
        saves.push(preferences.display.theme);
        familySaves.push(preferences.display.theme_family);
      }
      await route.fulfill({ json: preferences });
    });
  };
  await bindPreferences(page);
  await page.routeWebSocket('**/api/ws', (ws) => {
    ws.send(JSON.stringify({ type: 'authenticated' }));
  });
  return { saves, familySaves, pageErrors, bindPreferences };
}

for (const width of [1440, 390]) {
  for (const family of ['porcelain', 'classic', 'gruvbox', 'paper'] as const) {
    test(`${family} Light/Dark/System account appearance at ${width}px`, async ({ page }) => {
      await page.setViewportSize({ width, height: width === 390 ? 844 : 1000 });
      await page.emulateMedia({ colorScheme: 'light', reducedMotion: 'reduce' });
      const fixture = await installAppearanceFixture(page);
      await page.goto('/settings?tab=display');
      const root = page.locator('html');
      const familySelect = page.getByRole('combobox', { name: /family/i });
      const modeSelect = page.getByRole('combobox', { name: /^Appearance\b/ });
      await expect(familySelect).toBeVisible({ timeout: 30_000 });
      await expect(familySelect.locator('option')).toHaveText(THEME_FAMILIES.map((entry) => entry.name));
      await familySelect.selectOption(family);
      await expect(root).toHaveAttribute('data-theme-family', family);
      for (const mode of ['light', 'dark'] as const) {
        await modeSelect.selectOption(mode);
        await expect.poll(() => fixture.saves[fixture.saves.length - 1]).toBe(mode);
        await expect(root).toHaveAttribute('data-theme-family', family);
        await expect(root).toHaveAttribute('data-resolved-theme', mode);
        await textContrast(modeSelect);
        await page.reload();
        await expect(familySelect).toHaveValue(family);
        await expect(modeSelect).toHaveValue(mode);
        await expect(root).toHaveAttribute('data-theme-family', family);
        await expect(root).toHaveAttribute('data-resolved-theme', mode);
        if (family === 'gruvbox' && mode === 'light') {
          await expect.poll(() => root.evaluate((node) =>
            getComputedStyle(node).getPropertyValue('--color-canvas').trim())).toBe('251 241 199');
        }
        // Classic is the accepted pre-family semantic palette, not a recoloring.
        if (family === 'classic') {
          const roles = await root.evaluate((node) => {
            const css = getComputedStyle(node);
            return ['canvas', 'surface', 'foreground', 'accent'].map((role) =>
              css.getPropertyValue(`--color-${role}`).trim());
          });
          expect(roles).toEqual(mode === 'light'
            ? ['244 247 249', '255 255 255', '20 40 50', '3 111 159']
            : ['2 6 23', '15 23 42', '241 245 249', '56 189 248']);
        }
        await page.goto('/');
        const search = page.getByRole('textbox', { name: 'Search tasks and conversations' });
        await expect(search).toBeVisible({ timeout: 30_000 });
        await expect(root).toHaveAttribute('data-theme-family', family);
        await textContrast(search);
        await search.focus();
        await page.keyboard.press('Tab');
        await page.keyboard.press('Shift+Tab');
        await expect(search).toBeFocused();
        await textContrast(search, 3, '--tw-ring-color');
        await page.getByRole('heading', { name: 'Tasks', exact: true }).click();
        await noOverflow(page);
        await page.screenshot({ path: `/tmp/cognis-family-${family}-${mode}-${width}.png`, fullPage: true });
        await page.getByTestId(`dashboard-task-row-${TASK_ID}`).click();
        const dialog = page.getByRole('dialog').filter({ visible: true }).last();
        await expect(dialog).toBeVisible();
        await textContrast(dialog.getByRole('heading').first());
        const action = dialog.getByRole('button', { name: 'Open full page', exact: true });
        await expect(action).toBeVisible();
        await textContrast(action);
        await noOverflow(page);
        await page.screenshot({ path: `/tmp/cognis-family-${family}-${mode}-${width}-dialog.png` });
        await page.keyboard.press('Escape');
        await expect(dialog).toBeHidden();
        await page.goto('/settings?tab=display');
        await expect(familySelect).toHaveValue(family);
      }
      await modeSelect.selectOption('system');
      await expect.poll(() => fixture.saves[fixture.saves.length - 1]).toBe('system');
      await page.emulateMedia({ colorScheme: 'dark' });
      await expect(root).toHaveAttribute('data-resolved-theme', 'dark');
      await page.emulateMedia({ colorScheme: 'light' });
      await expect(root).toHaveAttribute('data-resolved-theme', 'light');
      await expect(root).toHaveAttribute('data-theme-family', family);
      await page.reload();
      await expect(familySelect).toHaveValue(family);
      await expect(modeSelect).toHaveValue('system');
      await expect(root).toHaveAttribute('data-resolved-theme', 'light');
      expect(fixture.pageErrors).toEqual([]);
    });
  }
}

async function noOverflow(page: Page) {
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(
    (await page.viewportSize())!.width,
  );
}

test('family changes preserve a live task chat draft and mounted message nodes', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 1000 });
  const fixture = await installAppearanceFixture(page);
  await page.goto(`/tasks/${TASK_ID}`);
  await page.getByTestId('task-agent-dock-launcher').click();
  const chat = page.getByTestId('task-control-native-chat');
  await expect(chat).toBeVisible();
  const message = chat.getByText('The release is waiting for your approval.', { exact: false });
  await expect(message).toBeVisible();
  const original = await message.elementHandle();
  const composer = chat.getByTestId('task-control-composer');
  await composer.fill('Release note sample:\n\n```python\nrelease_count = 3\nprint("ready")\n```');
  await chat.getByRole('button', { name: 'Send task control message' }).click();
  await expect(composer).toHaveValue('');
  const code = chat.locator('pre code').filter({ hasText: 'release_count' }).last();
  await expect(code).toBeVisible();
  await page.route('**/api/v1/artifacts/upload', (route) => route.fulfill({
    json: { artifact_id: 'artifact-release-note-fixture', kind: 'file', mime_type: 'text/plain',
      filename: 'release-notes.txt', size_bytes: 19 },
  }));
  await chat.locator('input[type="file"]').setInputFiles({
    name: 'release-notes.txt', mimeType: 'text/plain', buffer: Buffer.from('Release note draft.'),
  });
  const attachment = chat.getByText('release-notes.txt', { exact: true });
  await expect(attachment).toBeVisible();
  await composer.fill('Keep this unsent release note across family changes.');
  const settings = await page.context().newPage();
  const settingsFixture = await installAppearanceFixture(settings);
  await fixture.bindPreferences(settings);
  await settings.goto('/settings?tab=display');
  const familySelect = settings.getByRole('combobox', { name: /family/i });
  await expect(familySelect).toBeVisible({ timeout: 30_000 });
  for (const family of ['classic', 'gruvbox', 'paper', 'porcelain']) {
    await familySelect.selectOption(family);
    await expect.poll(() => fixture.familySaves[fixture.familySaves.length - 1]).toBe(family);
    for (const mode of ['light', 'dark']) {
      await settings.getByRole('combobox', { name: /^Appearance\b/ }).selectOption(mode);
      await expect.poll(() => fixture.saves[fixture.saves.length - 1]).toBe(mode);
      await page.bringToFront();
      // Revalidate through the real account preference store, not root mutation.
      await page.evaluate(() => window.dispatchEvent(new Event('focus')));
      await expect(page.locator('html')).toHaveAttribute('data-theme-family', family);
      await expect(page.locator('html')).toHaveAttribute('data-resolved-theme', mode);
      await expect(composer).toHaveValue('Keep this unsent release note across family changes.');
      expect(await message.evaluate((node, previous) => node === previous, original)).toBe(true);
      await textContrast(code);
      await expect(attachment).toBeVisible();
      await textContrast(attachment);
      await textContrast(chat.getByRole('button', { name: 'Send task control message' }));
      if (family === 'paper') {
        await expect(message).toHaveCSS('font-size', '15px');
        await expect(composer).toHaveCSS('font-size', '15px');
      }
      await page.screenshot({ path: `/tmp/cognis-family-${family}-${mode}-live-chat.png` });
      await settings.bringToFront();
    }
  }
  expect(fixture.pageErrors).toEqual([]);
  expect(settingsFixture.pageErrors).toEqual([]);
  await settings.close();
});

async function textContrast(locator: Locator, minimum = 4.5, property = 'color') {
  const ratio = await locator.evaluate((node, colorProperty) => {
    const rgba = (value: string) => {
      const parts = value.match(/[\d.]+/g)?.map(Number) ?? [];
      return [parts[0] ?? 0, parts[1] ?? 0, parts[2] ?? 0, parts[3] ?? 1];
    };
    const over = (front: number[], back: number[]) => [
      ...front.slice(0, 3).map((channel, index) => channel * front[3] + back[index] * (1 - front[3])),
      1,
    ];
    const backgrounds: number[][] = [];
    for (let ancestor: Element | null = node; ancestor; ancestor = ancestor.parentElement) {
      backgrounds.push(rgba(getComputedStyle(ancestor).backgroundColor));
    }
    const background = backgrounds.reverse().reduce((back, front) => over(front, back), [255, 255, 255, 1]);
    const foreground = over(rgba(getComputedStyle(node).getPropertyValue(colorProperty)), background);
    const luminance = (color: number[]) => color.slice(0, 3).reduce((sum, channel, index) => {
      const value = channel / 255;
      const linear = value <= 0.04045 ? value / 12.92 : ((value + 0.055) / 1.055) ** 2.4;
      return sum + linear * [0.2126, 0.7152, 0.0722][index];
    }, 0);
    const a = luminance(foreground);
    const b = luminance(background);
    return (Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05);
  }, property);
  expect(ratio).toBeGreaterThanOrEqual(minimum);
}

test('tool output stays legible in each family and mode through real account refresh', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 1000 });
  const fixture = await installAppearanceFixture(page);
  await page.goto(`/tasks/${TASK_ID}`);
  await page.getByTestId('task-cockpit-step-fetch').getByRole('button', { name: 'Result' }).click();
  const output = page.getByRole('dialog', { name: 'Step: fetch', exact: true });
  await expect(output).toContainText('Fetched release evidence');
  const settings = await page.context().newPage();
  const settingsFixture = await installAppearanceFixture(settings);
  await fixture.bindPreferences(settings);
  await settings.goto('/settings?tab=display');
  for (const family of THEME_FAMILIES) {
    await settings.getByRole('combobox', { name: /family/i }).selectOption(family.id);
    for (const mode of ['light', 'dark']) {
      await settings.getByRole('combobox', { name: /^Appearance\b/ }).selectOption(mode);
      await expect.poll(() => fixture.saves[fixture.saves.length - 1]).toBe(mode);
      await page.bringToFront();
      await page.evaluate(() => window.dispatchEvent(new Event('focus')));
      await expect(page.locator('html')).toHaveAttribute('data-theme-family', family.id);
      await expect(page.locator('html')).toHaveAttribute('data-resolved-theme', mode);
      // A task foreground refresh may close the output overlay. Reopen it
      // through its real action before checking rendered output colors.
      if (!await output.isVisible()) {
        await page.getByTestId('task-cockpit-step-fetch').getByRole('button', { name: 'Result' }).click();
      }
      await expect(output).toBeVisible();
      await textContrast(output.getByRole('heading').first());
      await textContrast(output.getByText('All required checks passed. Artifact digest: sha256:verified-stage39.', { exact: true }));
      await page.screenshot({ path: `/tmp/cognis-family-${family.id}-${mode}-tool-output.png` });
      await settings.bringToFront();
    }
  }
  expect(fixture.pageErrors).toEqual([]);
  expect(settingsFixture.pageErrors).toEqual([]);
  await settings.close();
});

for (const width of [1440, 390]) {
  test(`real account appearance, dashboard and dialogs at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: width === 390 ? 844 : 1000 });
    await page.emulateMedia({ colorScheme: 'light', reducedMotion: 'reduce' });
    const fixture = await installAppearanceFixture(page);
    const root = page.locator('html');
    await page.goto('/settings?tab=display');
    const theme = page.getByRole('combobox', { name: /^Appearance\b/ });
    await expect(theme).toHaveValue('system', { timeout: 30_000 });
    await expect(root).toHaveAttribute('data-resolved-theme', 'light');

    for (const mode of ['dark', 'light'] as const) {
      // Exercise the real settings event -> account preference save -> resolver.
      await theme.selectOption(mode);
      await expect.poll(() => fixture.saves[fixture.saves.length - 1]).toBe(mode);
      await expect(root).toHaveAttribute('data-resolved-theme', mode);
      await textContrast(theme);
      await noOverflow(page);
      const savedToast = page.getByRole('button').filter({ hasText: 'Preferences saved' });
      await expect(savedToast).toBeVisible();
      await savedToast.click();
      await expect(savedToast).toBeHidden();
      await page.screenshot({ path: `/tmp/cognis-theme-settings-${mode}-${width}.png`, fullPage: true });
      await page.reload();
      await expect(theme).toHaveValue(mode);
      await expect(root).toHaveAttribute('data-resolved-theme', mode);

      await page.goto('/');
      const search = page.getByRole('textbox', { name: 'Search tasks and conversations' });
      await expect(search).toBeVisible({ timeout: 30_000 });
      await expect(root).toHaveAttribute('data-resolved-theme', mode);
      await textContrast(search);
      await search.focus();
      await page.keyboard.press('Tab');
      await page.keyboard.press('Shift+Tab');
      await expect(search).toBeFocused();
      expect(await search.evaluate((node) => getComputedStyle(node).boxShadow)).not.toBe('none');
      await textContrast(search, 3, '--tw-ring-color');
      await noOverflow(page);
      await page.screenshot({ path: `/tmp/cognis-theme-dashboard-focus-${mode}-${width}.png`, fullPage: true });
      await page.getByRole('heading', { name: 'Tasks', exact: true }).click();
      await expect(search).not.toBeFocused();
      await expect(page.getByText('Could not load notifications', { exact: true })).toHaveCount(0);
      await page.screenshot({ path: `/tmp/cognis-theme-dashboard-${mode}-${width}.png`, fullPage: true });
      await page.getByTestId(`dashboard-task-row-${TASK_ID}`).click();
      const dialog = page.getByRole('dialog').filter({ visible: true }).last();
      await expect(dialog).toBeVisible();
      await textContrast(dialog.getByRole('heading').first());
      await noOverflow(page);
      await page.screenshot({ path: `/tmp/cognis-theme-dialog-${mode}-${width}.png` });
      await page.keyboard.press('Escape');
      await expect(dialog).toBeHidden();
      await page.goto('/settings?tab=display');
      await expect(theme).toHaveValue(mode);
    }

    await theme.selectOption('system');
    await expect.poll(() => fixture.saves[fixture.saves.length - 1]).toBe('system');
    await page.emulateMedia({ colorScheme: 'dark' });
    await expect(root).toHaveAttribute('data-resolved-theme', 'dark');
    await page.emulateMedia({ colorScheme: 'light' });
    await expect(root).toHaveAttribute('data-resolved-theme', 'light');
    await page.reload();
    await expect(theme).toHaveValue('system');
    await expect(root).toHaveAttribute('data-resolved-theme', 'light');
    await page.emulateMedia({ colorScheme: 'dark' });
    await expect(root).toHaveAttribute('data-resolved-theme', 'dark');
    expect(fixture.pageErrors).toEqual([]);
  });

  test(`shared-control preview screenshots at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: width === 390 ? 844 : 1000 });
    const fixture = await installAppearanceFixture(page);
    for (const mode of ['light', 'dark'] as const) {
      await page.goto(`/theme-preview?theme=${mode}`);
      const appearance = page.getByRole('combobox', { name: 'Appearance', exact: true });
      await expect(appearance).toHaveValue(mode);
      await expect(page.locator('html')).toHaveAttribute('data-resolved-theme', mode);
      // A late account/preferences refresh must not overwrite the fixture.
      await page.evaluate(() => window.dispatchEvent(new Event('focus')));
      await expect.poll(() => page.locator('html').getAttribute('data-resolved-theme')).toBe(mode);
      const approve = page.getByRole('button', { name: 'Approve', exact: true });
      await expect(approve).toBeVisible();
      await textContrast(approve);
      await noOverflow(page);
      await page.screenshot({ path: `/tmp/cognis-theme-preview-${mode}-${width}.png`, fullPage: true });
      await appearance.selectOption(mode === 'dark' ? 'light' : 'dark');
      await expect(page.locator('html')).toHaveAttribute('data-resolved-theme', mode === 'dark' ? 'light' : 'dark');
      await appearance.selectOption('system');
      await page.emulateMedia({ colorScheme: 'dark' });
      await expect(page.locator('html')).toHaveAttribute('data-resolved-theme', 'dark');
      await page.emulateMedia({ colorScheme: 'light' });
      await expect(page.locator('html')).toHaveAttribute('data-resolved-theme', 'light');
    }
    expect(fixture.pageErrors).toEqual([]);
  });
}
