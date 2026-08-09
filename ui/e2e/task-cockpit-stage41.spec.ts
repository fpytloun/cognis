import { expect, test } from '@playwright/test';

import { login } from './helpers';
import { installTaskCockpitFixture, TASK_ID } from './task-cockpit-fixture';

test.describe('Stage 41 decision-first Task Cockpit', () => {
  test.use({ serviceWorkers: 'block' });

  test('keeps one decision surface and moves it into the task-scoped dock', async ({ page }) => {
    await login(page);
    const fixture = await installTaskCockpitFixture(page);
    fixture.setStatus('paused');
    await page.goto(`/tasks/${TASK_ID}`);
    await expect(page.getByTestId('task-cockpit-surface')).toHaveAttribute(
      'data-source-tree',
      process.env.STAGE41_SOURCE_TREE ?? ''
    );

    const launcher = page.getByTestId('task-agent-dock-launcher');
    await expect(launcher).toBeVisible();
    await expect(page.getByTestId('task-attention')).toHaveCount(1);
    await expect(page.getByRole('button', { name: 'Review decision' })).toBeVisible();
    await page.getByRole('button', { name: 'Review decision' }).click();
    await expect(page.locator('#attention')).toBeInViewport();
    await expect(page.locator('#attention').getByRole('textbox')).toBeFocused();
    await expect(launcher).toHaveAccessibleName(/Open .+ for task Release safety review/);
    await launcher.click();
    await expect(page.getByTestId('task-agent-dock')).toBeVisible();
    await expect(page.getByTestId('task-agent-dock')).toBeFocused();
    await expect(page.getByTestId('task-attention')).toHaveCount(1);
    await expect(page.getByTestId('task-control-native-chat')).toBeVisible();
    await expect(page.getByTestId('task-agent-dock').locator('iframe')).toHaveCount(0);
    const chatTab = page.getByRole('tab', { name: 'Chat' });
    const workTab = page.getByRole('tab', { name: 'Work' });
    await expect(chatTab).toHaveAttribute('aria-controls', 'task-agent-panel-chat');
    await chatTab.focus();
    await page.keyboard.press('ArrowRight');
    await expect(workTab).toBeFocused();
    await expect(page.getByRole('tabpanel')).toHaveAttribute('id', 'task-agent-panel-work');

    await page.getByRole('button', { name: 'Expand agent to full screen' }).click();
    await expect(page.getByTestId('task-agent-dock')).toHaveAttribute('aria-modal', 'true');
    await expect(page.getByTestId('task-cockpit-surface')).toHaveAttribute('inert', '');
    await expect(page.getByRole('button', { name: 'Return to task' })).toBeVisible();
    await page.getByRole('tab', { name: 'Chat' }).click();
    const nativeChat = page.getByTestId('task-control-native-chat');
    await nativeChat.evaluate((root) => {
      const elements = Array.from(document.querySelectorAll<HTMLElement>(
        'button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])'
      )).filter((element) => root.contains(element) && element.getClientRects().length > 0);
      elements[elements.length - 1]?.focus();
    });
    await page.keyboard.press('Tab');
    await expect.poll(() => page.evaluate(() => {
      const dock = document.querySelector('[data-testid="task-agent-dock"]');
      return Boolean(dock?.contains(document.activeElement));
    })).toBe(true);
    await nativeChat.evaluate((root) => {
      const elements = Array.from(document.querySelectorAll<HTMLElement>(
        'button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])'
      )).filter((element) => root.contains(element) && element.getClientRects().length > 0);
      elements[0]?.focus();
    });
    await page.keyboard.press('Shift+Tab');
    await expect.poll(() => page.evaluate(() => {
      const dock = document.querySelector('[data-testid="task-agent-dock"]');
      return Boolean(dock?.contains(document.activeElement));
    })).toBe(true);
    await page.getByTestId('task-agent-dock').getByRole('button', { name: 'Return to task' }).click();
    await expect(page.getByTestId('task-agent-dock')).toHaveAttribute('aria-modal', 'false');
    await page.getByRole('button', { name: 'Minimize agent dock' }).click();
    await expect(launcher).toBeFocused();
    await launcher.click();
    await page.keyboard.press('Escape');
    await expect(launcher).toBeFocused();
  });

  test('converges the page, badge, and dock after a dock decision', async ({ page }) => {
    await login(page);
    const fixture = await installTaskCockpitFixture(page);
    fixture.setStatus('paused');
    await page.goto(`/tasks/${TASK_ID}`);
    await page.getByTestId('task-agent-dock-launcher').click();
    await page.getByTestId('task-agent-dock').getByRole('button', { name: 'Approve' }).click();

    await expect(page.getByTestId('task-attention')).toHaveCount(0);
    await expect(page.getByText('running', { exact: true }).first()).toBeVisible();
    await page.getByRole('button', { name: 'Minimize agent dock' }).click();
    await expect(page.getByLabel('1 pending decision')).toHaveCount(0);
    expect(fixture.actionRequests()).toContain(`POST /api/v1/tasks/${TASK_ID}/gate-response`);
  });

  test('renders activity before workflow and the canonical result before work details', async ({ page }) => {
    await login(page);
    const fixture = await installTaskCockpitFixture(page);
    fixture.setStatus('running');
    await page.goto(`/tasks/${TASK_ID}`);
    await expect(page.getByTestId('task-activity')).toBeVisible();
    const activityBox = await page.getByTestId('task-activity').boundingBox();
    const workflowBox = await page.getByTestId('task-cockpit-phase-rail').boundingBox();
    expect(activityBox!.y).toBeLessThan(workflowBox!.y);

    fixture.setStatus('completed');
    await page.reload();
    await expect(page.getByTestId('task-final-result')).toContainText('Release approved');
    await expect(page.getByText('This task has not produced a final result yet.')).toHaveCount(0);
  });

  test('uses a mobile bottom sheet and keeps the avatar launcher above navigation', async ({ page }) => {
    await login(page);
    const fixture = await installTaskCockpitFixture(page);
    fixture.setStatus('paused');
    await page.setViewportSize({ width: 390, height: 844 });
    await page.goto(`/tasks/${TASK_ID}`);
    const launcher = page.getByTestId('task-agent-dock-launcher');
    const box = await launcher.boundingBox();
    expect(box?.width).toBeGreaterThanOrEqual(44);
    expect(box?.height).toBeGreaterThanOrEqual(44);
    await page.keyboard.press('a');
    await expect(page.getByTestId('task-agent-dock')).toBeVisible();
    await expect(page.getByTestId('task-agent-dock')).toHaveCSS('width', '390px');
    await expect(page.getByTestId('task-agent-dock')).toHaveAttribute('aria-modal', 'true');
    await expect(page.getByTestId('task-cockpit-surface')).toHaveAttribute('inert', '');
    await expect(page.getByRole('navigation', { name: 'Primary' })).toBeHidden();
    const composer = page.getByTestId('task-control-composer');
    await expect(composer).toBeVisible();
    const composerBox = await composer.boundingBox();
    expect(composerBox!.y + composerBox!.height).toBeLessThanOrEqual(844);
  });
});
