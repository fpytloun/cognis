import { expect, test, type Locator, type Page } from '@playwright/test';
import type { BackgroundCommandDetail } from '../src/lib/types/api';

const commandDetail: BackgroundCommandDetail = {
  shell_id: 'drawer-command', executor_id: 'drawer-executor', session_id: null,
  status: 'running', command: 'npm test', cwd: '/workspace', pid: null,
  runtime_seconds: 10, idle_seconds: 0, exit_code: null,
  output_tail: 'Synthetic verification output',
  output_chars: 'Synthetic verification output'.length, trimmed_chars: 0,
};

async function expectSingleLineScroller(title: Locator, page: Page) {
  await title.scrollIntoViewIfNeeded();
  const metrics = await title.evaluate(element => {
    const style = getComputedStyle(element);
    return {
      width: element.clientWidth, content: element.scrollWidth,
      height: element.getBoundingClientRect().height, line: parseFloat(style.lineHeight),
      scrollbar: style.scrollbarWidth,
    };
  });
  expect(metrics.content).toBeGreaterThan(metrics.width);
  expect(metrics.height).toBeLessThanOrEqual(metrics.line + 2);
  expect(metrics.scrollbar).toBe('none');
  await title.hover();
  await page.mouse.wheel(350, 0);
  await expect.poll(() => title.evaluate(element => element.scrollLeft)).toBeGreaterThan(0);
}

for (const viewport of [
  { width: 393, height: 852 }, { width: 393, height: 480 }, { width: 1440, height: 900 },
]) {
  test(`ongoing drawer separates dense sections and scrolls titles at ${viewport.width}x${viewport.height}`, async ({ page }, testInfo) => {
    await page.setViewportSize(viewport);
    await page.emulateMedia({ colorScheme: 'dark', reducedMotion: 'reduce' });
    const errors: string[] = [];
    page.on('pageerror', error => errors.push(error.message));
    const commands: string[] = [];
    await page.route(url => url.pathname.startsWith('/api/'), async route => {
      const path = new URL(route.request().url()).pathname;
      if (path.endsWith('/auth/me')) {
        await route.fulfill({ json: { email: 'drawer-acceptance@cognis.test', role: 'user' } });
        return;
      }
      if (path.includes('/background-commands/')) commands.push(path);
      await route.fulfill({ json: commandDetail });
    });
    await page.goto('/?surface=ongoing');
    const drawer = page.getByTestId('drawer-under-test');
    await expect.poll(async () => errors.length || await drawer.count()).toBeGreaterThan(0);
    expect(errors).toEqual([]);
    const todos = drawer.getByRole('list').first();
    const workHeading = drawer.getByText('Tasks, sessions, and commands', { exact: true });
    const assertNoOverlap = async () => {
      const lastTodo = (await todos.getByRole('listitem').last().boundingBox())!;
      const heading = (await workHeading.boundingBox())!;
      if (viewport.width < 1024) {
        expect(lastTodo.y + lastTodo.height).toBeLessThanOrEqual(heading.y);
      } else {
        const list = (await todos.boundingBox())!;
        expect(list.x + list.width).toBeLessThanOrEqual(heading.x);
      }
      expect(await page.evaluate(() => document.documentElement.scrollWidth - innerWidth)).toBeLessThanOrEqual(1);
      const panel = (await drawer.boundingBox())!;
      for (const element of [
        drawer.getByRole('button', { name: /^Ongoing work/ }),
        ...await drawer.getByRole('article').all(),
      ]) {
        const bounds = (await element.boundingBox())!;
        expect(bounds.x + bounds.width).toBeLessThanOrEqual(panel.x + panel.width + 1);
      }
    };
    await assertNoOverlap();
    for (const prefix of ['Managed subsession:', 'Delegate subsession:', 'Unavailable subsession:', 'Command:', 'Task:', 'Todo 1:']) {
      await expectSingleLineScroller(drawer.getByTitle(new RegExp(`^${prefix}`)), page);
    }
    expect(commands).toEqual([]);
    await expect(drawer.getByRole('link', { name: /^Managed subsession:/ })).toHaveAttribute('href', '/chat/drawer-managed');
    await expect(drawer.getByRole('link', { name: /^Task:/ })).toHaveAttribute('href', '/tasks/drawer-task');
    await expect(page.getByTestId('drawer-opened-session')).toHaveText('');
    const delegateTitle = await drawer.getByTitle(/^Delegate subsession:/).innerText();
    await drawer.getByRole('button', { name: delegateTitle, exact: true }).click();
    await expect(page.getByTestId('drawer-opened-session')).toHaveText('drawer-delegate-session');
    await drawer.getByRole('button', { name: /^Command:/ }).click();
    await expect.poll(() => commands.length).toBe(1);
    await expect(drawer.getByText('Synthetic verification output')).toBeVisible();
    await assertNoOverlap();
    await expect(page.getByTestId('drawer-composer')).toBeInViewport();
    await page.screenshot({ path: testInfo.outputPath(`ongoing-drawer-${viewport.width}x${viewport.height}.png`) });
    expect(errors).toEqual([]);
  });
}

for (const width of [393, 1440]) {
  for (const presentation of ['overlay', 'focus', 'pinned']) {
    test(`inspector tabs have no fade or header separators in ${presentation} at ${width}px`, async ({ page }, testInfo) => {
      await page.setViewportSize({ width, height: 852 });
      await page.emulateMedia({ colorScheme: 'dark', reducedMotion: 'reduce' });
      await page.route(url => url.pathname.startsWith('/api/'), route =>
        route.fulfill({ json: { email: 'drawer-acceptance@cognis.test', role: 'user' } }));
      await page.goto(`/?surface=inspector&presentation=${presentation}`);
      const tabs = page.getByRole('tablist', { name: 'Session inspector' });
      await expect(tabs).toBeVisible();
      await expect(page.getByTestId('tabs-edge-fade')).toHaveCount(0);
      for (const wrapper of [tabs.locator('..'), page.getByTestId('conversation-info-header').locator('..')]) {
        expect(await wrapper.evaluate(element => getComputedStyle(element).borderBottomWidth)).toBe('0px');
      }
      const overview = tabs.getByRole('tab', { name: 'Overview' });
      expect(await overview.evaluate(element => parseFloat(getComputedStyle(element).borderBottomWidth))).toBeGreaterThan(0);
      await overview.focus();
      await page.keyboard.press('ArrowRight');
      await expect(tabs.getByRole('tab', { name: 'Work' })).toHaveAttribute('aria-selected', 'true');
      await page.keyboard.press('End');
      await expect(tabs.getByRole('tab', { name: 'Session' })).toHaveAttribute('aria-selected', 'true');
      await page.screenshot({ path: testInfo.outputPath(`inspector-${presentation}-${width}.png`) });
      if (presentation === 'overlay') await page.getByRole('button', { name: 'Close conversation information', exact: true }).click();
      else await page.keyboard.press('Escape');
      await expect(tabs).toHaveCount(0);
    });
  }
}
