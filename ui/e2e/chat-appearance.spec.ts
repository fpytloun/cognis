import { expect, test, type Locator } from '@playwright/test';
import { COMPONENT_RUNTIME_ORIGIN } from './component-runtime';

async function textContrast(locator: Locator): Promise<number> {
  return locator.evaluate((element) => {
    const rgba = (value: string) => {
      const channels = value.match(/[\d.]+/g)?.map(Number) ?? [];
      return [channels[0] ?? 0, channels[1] ?? 0, channels[2] ?? 0, channels[3] ?? 1];
    };
    const composite = (front: number[], back: number[]) =>
      front.slice(0, 3).map((channel, index) => channel * front[3] + back[index] * (1 - front[3]));
    const ancestors: Element[] = [];
    for (let current: Element | null = element; current; current = current.parentElement) ancestors.push(current);
    let background = [255, 255, 255];
    for (const ancestor of ancestors.reverse()) background = composite(rgba(getComputedStyle(ancestor).backgroundColor), background);
    const foreground = composite(rgba(getComputedStyle(element).color), background);
    const luminance = (color: number[]) => color
      .map((channel) => channel / 255)
      .map((channel) => channel <= 0.04045 ? channel / 12.92 : ((channel + 0.055) / 1.055) ** 2.4)
      .reduce((value, channel, index) => value + channel * [0.2126, 0.7152, 0.0722][index], 0);
    const values = [luminance(foreground), luminance(background)].sort((a, b) => a - b);
    return (values[1] + 0.05) / (values[0] + 0.05);
  });
}

for (const width of [1440, 390]) {
  for (const theme of ['light', 'dark'] as const) {
    test(`real chat controls and terminal contrast in ${theme} at ${width}px`, async ({ page }) => {
      await page.setViewportSize({ width, height: 1000 });
      await page.emulateMedia({ colorScheme: theme, reducedMotion: 'reduce' });
      await page.goto(`${COMPONENT_RUNTIME_ORIGIN}/scoped.html?theme-preview`);
      await expect(page.locator('html')).toHaveAttribute('data-resolved-theme', theme);
      const preview = page.getByTestId('chat-theme-preview');
      await expect(preview).toBeVisible();
      await expect(preview.getByText('review-notes.md', { exact: true })).toBeVisible();
      expect(await textContrast(preview.getByText('review-notes.md', { exact: true }))).toBeGreaterThanOrEqual(4.5);
      expect(await textContrast(preview.getByRole('button', { name: 'Download review-notes.md', exact: true }))).toBeGreaterThanOrEqual(4.5);
      const terminal = page.getByTestId('theme-terminal');
      const toggle = terminal.getByRole('button', { name: /bash.*npm test/s }).first();
      await toggle.click();
      for (const text of [
        'ANSI 37 white', 'ANSI 97 bright white', 'Error output',
        'Black on white', 'White on black', 'Indexed black on white',
        'Indexed white on black', 'White background only', 'Black background only',
      ]) {
        expect(await textContrast(terminal.getByText(text, { exact: true })), text).toBeGreaterThanOrEqual(4.5);
      }
      const note = preview.getByPlaceholder('Add context for this decision…');
      await note.fill('Please verify the test results.');
      const original = await preview.elementHandle();
      await page.emulateMedia({ colorScheme: theme === 'light' ? 'dark' : 'light' });
      await expect(page.locator('html')).toHaveAttribute('data-resolved-theme', theme === 'light' ? 'dark' : 'light');
      await expect(note).toHaveValue('Please verify the test results.');
      expect(await original?.evaluate((element) => element.isConnected)).toBe(true);
      await page.emulateMedia({ colorScheme: theme });
      await expect(page.locator('html')).toHaveAttribute('data-resolved-theme', theme);
      await toggle.click();
      await preview.getByRole('heading', { name: 'Chat appearance' }).click();
      expect(await preview.evaluate((element) => element.scrollWidth <= element.clientWidth)).toBe(true);
      await page.mouse.move(0, 0);
      await preview.screenshot({ path: `/tmp/cognis-theme-chat-${theme}-${width}.png`, animations: 'disabled' });
    });
  }
}
