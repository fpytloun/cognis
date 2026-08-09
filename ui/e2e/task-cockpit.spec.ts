import { expect, test } from '@playwright/test';

import { login } from './helpers';
import { installTaskCockpitFixture, TASK_ID } from './task-cockpit-fixture';

test.describe('Stage 39/41 production Task Cockpit', () => {
  test.use({ serviceWorkers: 'block' });
  test.setTimeout(90_000);

  test('desktop paused cockpit renders authoritative phases and lazy output', async ({ page }, testInfo) => {
    await login(page);
    const fixture = await installTaskCockpitFixture(page);
    fixture.setStatus('paused');
    await page.setViewportSize({ width: 1600, height: 1400 });
    await navigateCockpit(page);
    await page.evaluate(() => window.scrollTo(0, 0));

    await expect(page.getByRole('heading', { name: 'Release safety review' })).toBeVisible();
    await expect(page.getByTestId('task-cockpit-objective')).toHaveText(
      'An evidence-backed go/no-go decision with complete audit context.'
    );
    const rail = page.getByTestId('task-cockpit-phase-rail');
    await expect(rail.locator('a')).toHaveText([
      'Prepare · completed',
      'Investigate · completed',
      'Review · waiting'
    ]);
    await expect(page.getByTestId('task-cockpit-step-approve')).toHaveAttribute('data-step-status', 'waiting');
    await expect(page.getByTestId('task-cockpit-action-approve')).toHaveText('Action required');
    await expect(page.getByTestId('task-cockpit-step-no_changes')).toContainText('Skipped: condition:route:false');
    await expect(page.getByTestId('task-cockpit-step-fetch')).toContainText('Tool call · completed');
    await expect(page.getByRole('button', { name: 'Configure' })).toBeVisible();
    await expect(page.getByRole('button', { name: 'Ask', exact: true }).filter({ visible: true }).first()).toBeVisible();
    await expect(page.getByRole('button', { name: 'Cancel task' })).toBeVisible();
    await expect(page.getByText('Approve this release for production?')).toBeVisible();
    await expect(page.getByTestId('task-attention').getByRole('button', { name: 'Approve' })).toBeVisible();
    await expect(page.getByTestId('task-progress')).toContainText('Collect deterministic evidence');
    await expect(page.getByTestId('task-progress')).toContainText('Independent evidence review');

    expect(fixture.heavyRequests()).toBe(0);
    await page.getByTestId('task-cockpit-step-fetch').getByRole('button', { name: 'Output' }).click();
    await expect.poll(() => fixture.heavyRequests()).toBe(1);
    const output = page.getByRole('dialog', { name: 'fetch' });
    await expect(output).toContainText('Fetched release evidence');
    await expect(output).toContainText('sha256:verified-stage39');
    await output.getByRole('button', { name: 'Close' }).click();

    await assertFixtureHealthy(page, fixture);
    await navigateCockpit(page);
    const screenshotPath = testInfo.outputPath('task-cockpit-desktop-paused-overview.png');
    await page.locator('main').screenshot({ path: screenshotPath, animations: 'disabled' });
    await testInfo.attach('task-cockpit-desktop-paused', {
      path: screenshotPath,
      contentType: 'image/png'
    });

    const attention = page.getByRole('heading', { name: 'Approve this release for production?' })
      .locator('xpath=ancestor::div[contains(@class,"overflow-hidden")][1]');
    await attention.scrollIntoViewIfNeeded();
    await expect(attention.getByRole('button', { name: 'Approve' })).toBeVisible();
    await expect(attention.getByRole('button', { name: 'Reject' })).toBeVisible();
    const attentionPath = testInfo.outputPath('task-cockpit-desktop-attention-controls.png');
    await attention.screenshot({ path: attentionPath, animations: 'disabled' });
    await testInfo.attach('task-cockpit-desktop-attention-controls', {
      path: attentionPath,
      contentType: 'image/png'
    });

    await page.getByTestId('task-progress').scrollIntoViewIfNeeded();
    await expect(page.getByTestId('task-cockpit-phase-rail')).toBeVisible();
    const progressPath = testInfo.outputPath('task-cockpit-desktop-progress-phases.png');
    await page.locator('main').screenshot({ path: progressPath, animations: 'disabled' });
    await testInfo.attach('task-cockpit-desktop-progress-phases', {
      path: progressPath,
      contentType: 'image/png'
    });
  });

  test('mobile action sheet exposes lifecycle and parity actions across states', async ({ page }, testInfo) => {
    await login(page);
    const fixture = await installTaskCockpitFixture(page);
    await page.setViewportSize({ width: 390, height: 844 });

    const scenarios = [
      {
        status: 'draft' as const,
        buttons: ['Submit task', 'Configure task', 'Ask', 'Cancel task'],
        absent: ['Pause task', 'Resume task', 'Re-run task'],
        projection: { current: 'scope', status: 'pending', actionRequired: false }
      },
      {
        status: 'running' as const,
        buttons: ['Pause task', 'Configure task', 'Ask', 'Cancel task'],
        absent: ['Submit task', 'Resume task', 'Re-run task'],
        projection: { current: 'fetch', status: 'running', actionRequired: false }
      },
      {
        status: 'paused' as const,
        buttons: ['Resume task', 'Configure task', 'Ask', 'Re-run task', 'Cancel task'],
        absent: ['Submit task', 'Pause task'],
        projection: { current: 'approve', status: 'waiting', actionRequired: true }
      },
      {
        status: 'completed' as const,
        buttons: ['Configure task', 'Ask', 'Re-run task'],
        absent: ['Submit task', 'Pause task', 'Resume task', 'Cancel task'],
        projection: { current: 'finish', status: 'completed', actionRequired: false }
      }
    ];

    for (const scenario of scenarios) {
      const detailsBeforeNavigation = fixture.detailResponses().length;
      fixture.setStatus(scenario.status);
      await navigateCockpit(page);
      await page.getByRole('button', { name: 'More task actions' }).click();
      const sheet = page.getByRole('dialog', { name: 'Task actions' });
      await expect(sheet.locator('button:not([aria-label="Dismiss"])')).toHaveText(scenario.buttons);
      for (const absent of scenario.absent) {
        await expect(sheet.getByRole('button', { name: absent })).toHaveCount(0);
      }
      const currentStep = page.getByTestId(`task-cockpit-step-${scenario.projection.current}`);
      await expect(currentStep).toHaveAttribute('data-step-status', scenario.projection.status);
      await expect(page.getByTestId('task-cockpit-action-approve')).toHaveCount(
        scenario.projection.actionRequired ? 1 : 0
      );
      await currentStep.scrollIntoViewIfNeeded();
      await assertContainedInViewport(page, currentStep);
      await assertContainedInViewport(page, sheet);
      await assertDescendantsContained(page, currentStep);
      await assertDescendantsContained(page, sheet);
      if (scenario.status !== 'draft') {
        await expect.poll(() => fixture.detailResponses().length).toBeGreaterThan(detailsBeforeNavigation);
        const detail = fixture.detailResponses()[fixture.detailResponses().length - 1];
        expect(detail.status).toBe(scenario.status === 'paused' ? 'paused' : scenario.projection.status);
        if (scenario.status === 'running' || scenario.status === 'paused') {
          expect(detail.output).toBeNull();
        }
      }
      await assertFixtureHealthy(page, fixture);
      await page.keyboard.press('Escape');
    }

    fixture.setStatus('paused');
    await navigateCockpit(page);
    await page.getByRole('button', { name: 'More task actions' }).click();
    await page.getByRole('dialog', { name: 'Task actions' }).getByRole('button', { name: 'Configure task' }).click();
    await expect(page.getByRole('dialog', { name: 'Release safety review' })).toContainText('Task configuration');
    await page.keyboard.press('Escape');

    await page.getByRole('button', { name: 'More task actions' }).click();
    await page.getByRole('dialog', { name: 'Task actions' }).getByRole('button', { name: 'Ask', exact: true }).click();
    await expect(page.getByTestId('task-agent-dock')).toBeVisible();
    await expect(page.getByTestId('task-control-native-chat')).toBeVisible();
    await expect(page.getByTestId('task-agent-dock').locator('iframe')).toHaveCount(0);
    await page.getByRole('button', { name: 'Minimize agent dock' }).click();
    await page.getByRole('button', { name: 'More task actions' }).click();
    await page.getByRole('dialog', { name: 'Task actions' }).getByRole('button', { name: 'Ask', exact: true }).click();
    await expect(page.getByTestId('task-control-native-chat')).toBeVisible();
    expect(fixture.navigationRequests()).toEqual([
      `POST /api/v1/tasks/${TASK_ID}/control-chat`
    ]);
    await assertFixtureHealthy(page, fixture);

    await clickSheetAction(page, fixture, 'draft', 'Submit task', 'submit');
    await clickSheetAction(page, fixture, 'running', 'Pause task', 'pause');
    await clickSheetAction(page, fixture, 'running', 'Cancel task', 'cancel', true);
    await clickSheetAction(page, fixture, 'paused', 'Resume task', 'resume');
    await clickSheetAction(page, fixture, 'paused', 'Re-run task', 'rerun');
    await clickSheetAction(page, fixture, 'paused', 'Cancel task', 'cancel', true);
    await clickSheetAction(page, fixture, 'completed', 'Re-run task', 'rerun');
    expect(fixture.actionRequests()).toEqual([
      `POST /api/v1/tasks/${TASK_ID}/submit`,
      `POST /api/v1/tasks/${TASK_ID}/pause`,
      `POST /api/v1/tasks/${TASK_ID}/cancel`,
      `POST /api/v1/tasks/${TASK_ID}/resume`,
      `POST /api/v1/tasks/${TASK_ID}/rerun`,
      `POST /api/v1/tasks/${TASK_ID}/cancel`,
      `POST /api/v1/tasks/${TASK_ID}/rerun`
    ]);

    fixture.setStatus('paused');
    await navigateCockpit(page);
    await page.evaluate(() => window.scrollTo(0, 0));
    await expect(page.getByText('Approve this release for production?')).toBeVisible();
    await assertContainedInViewport(page, page.getByTestId('task-attention'));
    await assertDescendantsContained(page, page.getByTestId('task-attention'));
    await assertFixtureHealthy(page, fixture);
    const overviewPath = testInfo.outputPath('task-cockpit-mobile-paused-overview.png');
    await page.screenshot({ path: overviewPath, fullPage: true, animations: 'disabled' });
    await testInfo.attach('task-cockpit-mobile-paused-overview', {
      path: overviewPath,
      contentType: 'image/png'
    });

    await page.getByRole('button', { name: 'More task actions' }).click();
    await page.getByRole('dialog', { name: 'Task actions' }).getByRole('button', { name: 'Ask', exact: true }).click();
    const controlChat = page.getByTestId('task-agent-dock');
    await expect(controlChat).toContainText('Release safety review');
    await expect(controlChat).toContainText('Approve this release for production?');
    await expect(page.getByTestId('task-control-native-chat')).toBeVisible();
    const controlPath = testInfo.outputPath('task-cockpit-mobile-control-chat-reopened.png');
    await page.screenshot({ path: controlPath, animations: 'disabled' });
    await testInfo.attach('task-cockpit-mobile-control-chat-reopened', {
      path: controlPath,
      contentType: 'image/png'
    });
  });

  test('persistent Task Control Chat loads prior content and composer after reopen', async ({ page }, testInfo) => {
    await login(page);
    const fixture = await installTaskCockpitFixture(page);
    fixture.setStatus('paused');
    await page.setViewportSize({ width: 1440, height: 1000 });
    await navigateCockpit(page);

    const openControlChat = async () => {
      await page.getByTestId('task-agent-dock-launcher').click();
      await expect(page.getByTestId('task-agent-dock')).toBeVisible();
      await expect(page.getByTestId('task-control-native-chat')).toBeVisible();
      await assertFixtureHealthy(page, fixture);
      const chat = page.getByTestId('task-control-native-chat');
      await expect(chat.getByText('What is blocking this release?')).toBeVisible();
      await expect(chat.getByText('The release is waiting for your approval.', { exact: false })).toBeVisible();
      await expect(chat.locator('header')).toHaveCount(0);
      await expect(chat.locator('iframe')).toHaveCount(0);
      const composer = chat.getByTestId('task-control-composer');
      await expect(composer).toBeVisible();
      await composer.fill('Add a bounded release note.');
      await chat.getByRole('button', { name: 'Send task control message' }).click();
      await expect(composer).toHaveValue('');
      await composer.scrollIntoViewIfNeeded();
      await assertContainedInViewport(page, composer);
    };

    await openControlChat();
    const desktopPath = testInfo.outputPath('task-cockpit-desktop-control-chat.png');
    await page.getByTestId('task-agent-dock').screenshot({
      path: desktopPath,
      animations: 'disabled'
    });
    await testInfo.attach('task-cockpit-desktop-control-chat', {
      path: desktopPath,
      contentType: 'image/png'
    });

    await page.getByRole('button', { name: 'Minimize agent dock' }).click();
    await page.setViewportSize({ width: 390, height: 844 });
    await openControlChat();
    await page.waitForTimeout(500);
    const mobilePath = testInfo.outputPath('task-cockpit-mobile-control-chat-reopened.png');
    await page.screenshot({ path: mobilePath, animations: 'disabled' });
    await testInfo.attach('task-cockpit-mobile-control-chat-reopened', {
      path: mobilePath,
      contentType: 'image/png'
    });

    expect(fixture.navigationRequests()).toEqual([
      `POST /api/v1/tasks/${TASK_ID}/control-chat`
    ]);
    await assertFixtureHealthy(page, fixture);
  });

  test('terminal cockpit preserves deterministic history and renders canonical result before work', async ({ page }, testInfo) => {
    await login(page);
    const fixture = await installTaskCockpitFixture(page);
    fixture.setStatus('completed');
    await navigateCockpit(page);

    await expect(page.getByTestId('task-cockpit-phase-rail').locator('a')).toHaveText([
      'Prepare · completed',
      'Investigate · completed',
      'Review · completed'
    ]);
    await expect(page.getByTestId('task-cockpit-step-no_changes')).toContainText('condition:route:false');
    await expect(page.locator('p:visible').filter({ hasText: 'Release approved.' })).toBeVisible();
    await expect(page.getByTestId('task-work-compact')).toContainText('1');
    await expect(page.getByTestId('task-final-result')).toContainText('Release approved');
    expect(await page.getByTestId('task-final-result').evaluate((result) =>
      Boolean(result.compareDocumentPosition(document.querySelector('[data-testid="task-progress"]')!) & Node.DOCUMENT_POSITION_FOLLOWING)
    )).toBe(true);
    await page.getByTestId('task-work-compact').getByRole('button', { name: 'Explore' }).click();
    const work = page.getByRole('dialog', { name: 'Task work' });
    await expect(work.getByRole('tab', { name: /Files 1/ })).toBeVisible();
    await expect(work.getByRole('treeitem', { name: /cognis/ })).toBeVisible();
    await work.getByTestId('work-tab-results').click();
    await expect(work.getByTestId('work-primary-result')).toBeVisible();
    await expect(work.getByTestId('work-deliverable-dlv-stage39-final')).toBeVisible();
    await expect(work.getByRole('region', { name: 'Supporting results' })).toBeVisible();
    await work.getByTestId('work-tab-commands').click();
    await expect(work.getByText(/uv run pytest tests\/release -q/)).toBeVisible();
    await expect(work.getByRole('button', { name: 'Full output' })).toHaveCount(0);
    await expect(page.getByRole('button', { name: 'Re-run task' })).toBeVisible();
    await expect.poll(() => fixture.detailResponses().length).toBeGreaterThan(0);
    const selectedDetail = fixture.detailResponses()[fixture.detailResponses().length - 1];
    expect(selectedDetail.step_run_id).toBe('run-scope-1');
    expect(selectedDetail.status).toBe('completed');
    await assertFixtureHealthy(page, fixture);
    const screenshotPath = testInfo.outputPath('task-cockpit-desktop-completed-work.png');
    await page.screenshot({ path: screenshotPath, fullPage: true, animations: 'disabled' });
    await testInfo.attach('task-cockpit-desktop-completed-work', { path: screenshotPath, contentType: 'image/png' });
  });

  test('Chat v2 Work view renders the same canonical result and evidence', async ({ page }, testInfo) => {
    await login(page);
    const fixture = await installTaskCockpitFixture(page);
    await page.setViewportSize({ width: 1440, height: 1000 });
    await page.goto('/chat/conv-task-chat?view=work', { waitUntil: 'domcontentloaded' });
    const workAction = page.getByTestId('chat-header-work');
    await expect(workAction).toHaveAttribute('aria-label', 'Work');
    await expect(workAction).toHaveAttribute('title', 'Work');
    await expect(workAction).toHaveText('');
    const infoAction = page.getByRole('button', { name: 'Toggle session details' });
    await expect.poll(async () => {
      const [workBox, infoBox] = await Promise.all([workAction.boundingBox(), infoAction.boundingBox()]);
      return [workBox?.width === infoBox?.width, workBox?.height === infoBox?.height];
    }).toEqual([true, true]);
    await expect(page.getByRole('button', { name: 'Task', exact: true })).toBeVisible();
    if (await workAction.getAttribute('aria-expanded') !== 'true') {
      await workAction.click();
    }
    await expect(page.getByTestId('work-view')).toBeVisible();
    await expect(page.getByTestId('conversation-info-drawer')).toBeVisible();
    await page.getByRole('button', { name: 'Close conversation information' }).click();
    await workAction.click();
    await expect(page.getByTestId('conversation-info-drawer')).toBeVisible();
    await page.keyboard.press('Escape');
    await expect(page.getByTestId('conversation-info-drawer')).toHaveCount(0);
    await expect(workAction).toBeFocused();
    await infoAction.click();
    await expect(page.getByTestId('conversation-info-drawer')).toBeVisible();
    await page.getByTestId('conversation-info-full').click();
    await expect(page.getByText('stage39-model', { exact: true }).first()).toBeVisible();
    await page.keyboard.press('Escape');
    await expect(infoAction).toBeFocused();
    const contextAction = page.getByRole('button', { name: 'Open context usage details' });
    await contextAction.click();
    await expect(page.getByTestId('conversation-info-drawer')).toBeVisible();
    await page.keyboard.press('Escape');
    await expect(contextAction).toBeFocused();
    await workAction.click();
    await expect(page.getByTestId('work-view')).toBeVisible();
    await expect(page.getByRole('complementary', { name: 'Conversation list' })).toBeVisible();
    await expect.poll(async () => {
      const [shell, chat, drawer] = await Promise.all([
        page.getByTestId('chat-shell').boundingBox(),
        page.getByTestId('chat-main').boundingBox(),
        page.getByTestId('conversation-info-drawer').boundingBox()
      ]);
      return {
        usefulDrawerWidth: Boolean(drawer && drawer.width >= 360 && drawer.width <= 440),
        chatEndsBeforeDrawer: Boolean(chat && drawer && chat.x + chat.width <= drawer.x),
        shellShrinksChat: Boolean(shell && chat && chat.width < shell.width - 360)
      };
    }).toEqual({ usefulDrawerWidth: true, chatEndsBeforeDrawer: true, shellShrinksChat: true });
    const work = page.getByTestId('work-view');
    await expect(work.getByRole('treeitem', { name: /cognis/ })).toBeVisible();
    await expect(work.getByTestId('workstream-filters')).toBeVisible();
    await expect(work.getByTestId('work-graph-truncated')).toBeVisible();
    await work.getByTestId('work-load-older').click();
    await work.getByTestId('work-tab-commands').click();
    await expect(work.getByTestId('work-panel-commands').locator('[data-testid^="work-command-"]')).toHaveCount(100);
    await work.getByTestId('work-page-newer').click();
    await work.getByTestId('work-tab-results').click();
    await expect(work.getByTestId('work-deliverable-dlv-stage39-final')).toBeVisible();
    await work.getByTestId('work-tab-commands').click();
    await expect(work.getByText(/uv run pytest tests\/release -q/)).toBeVisible();
    await work.getByTestId('work-tab-mutations').click();
    await expect(work.getByText('Update release agent', { exact: true })).toBeVisible();
    await assertFixtureHealthy(page, fixture);
    await page.waitForTimeout(500);
    const screenshotPath = testInfo.outputPath('chat-v2-work.png');
    await work.screenshot({ path: screenshotPath });
    await testInfo.attach('chat-v2-work', { path: screenshotPath, contentType: 'image/png' });

    const mutations = work.getByTestId('work-panel-mutations');
    const mutationPath = testInfo.outputPath('chat-v2-work-mutations.png');
    await mutations.screenshot({ path: mutationPath, animations: 'disabled' });
    await testInfo.attach('chat-v2-work-mutations', { path: mutationPath, contentType: 'image/png' });
    await page.getByTestId('conversation-info-full').click();
    await expect(page.getByTestId('conversation-info-full')).toHaveAttribute('aria-selected', 'true');
    await page.evaluate(() => {
      const link = document.createElement('a');
      link.href = '/chat/conv-task-chat-b';
      link.dataset.testid = 'switch-conversation-b';
      link.textContent = 'Switch to conversation B';
      document.body.append(link);
    });
    await page.getByTestId('switch-conversation-b').dispatchEvent('click');
    await expect(page).toHaveURL(/\/chat\/conv-task-chat-b/);
    await expect(page.getByTestId('conversation-info-drawer')).toBeVisible();
    await expect(page.getByTestId('conversation-info-full')).toHaveAttribute('aria-selected', 'true');
    await expect(page.getByText('stage39-model-b', { exact: true }).first()).toBeVisible();
    expect(await page.locator('[id]').evaluateAll((elements) => {
      const ids = elements.map((element) => element.id).filter(Boolean);
      return new Set(ids).size === ids.length;
    })).toBe(true);
  });

  test('installed PWA header keeps Search and Info across portrait, landscape, and tablet', async ({ page }, testInfo) => {
    await login(page);
    const fixture = await installTaskCockpitFixture(page);
    await page.addInitScript(() => {
      Object.defineProperty(window.navigator, 'standalone', { configurable: true, value: true });
      const nativeMatchMedia = window.matchMedia.bind(window);
      window.matchMedia = (query: string) => query === '(display-mode: standalone)'
        ? {
            matches: true,
            media: query,
            onchange: null,
            addListener: () => undefined,
            removeListener: () => undefined,
            addEventListener: () => undefined,
            removeEventListener: () => undefined,
            dispatchEvent: () => true,
          } as MediaQueryList
        : nativeMatchMedia(query);
    });
    await page.setViewportSize({ width: 390, height: 844 });
    await page.goto('/chat/conv-task-chat', { waitUntil: 'domcontentloaded' });
    await expect(page.getByRole('button', { name: 'Search conversation', exact: true })).toBeVisible();
    await expect(page.getByRole('button', { name: 'Toggle session details' })).toBeVisible();
    await expect(page.getByTestId('chat-header-work')).toHaveCount(0);
    await page.getByRole('button', { name: 'Toggle session details' }).click();
    const infoButton = page.getByRole('button', { name: 'Toggle session details' });
    const infoDialog = page.getByRole('dialog', { name: 'Conversation information' });
    await expect(infoButton).toHaveAttribute('aria-controls', 'conversation-info-drawer');
    await expect(infoButton).toHaveAttribute('aria-expanded', 'true');
    await expect(infoDialog).toBeVisible();
    await page.keyboard.press('Escape');
    await expect(infoDialog).toHaveCount(0);
    await expect(infoButton).toBeFocused();
    await infoButton.click();
    await expect(page.getByTestId('conversation-info-full')).toBeVisible();
    await expect(page.getByTestId('conversation-info-star')).toBeVisible();
    await page.getByTestId('conversation-info-work').click();
    await expect(page.getByTestId('work-view')).toBeVisible();
    await page.getByRole('button', { name: 'Dismiss' }).click({ position: { x: 2, y: 2 } });
    await expect(infoDialog).toHaveCount(0);
    await expect(infoButton).toBeFocused();
    await infoButton.click();
    await page.getByTestId('conversation-info-work').click();
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(390);
    for (const viewport of [
      { name: 'landscape', width: 844, height: 390 },
      { name: 'tablet', width: 1024, height: 1366 },
    ]) {
      await page.setViewportSize({ width: viewport.width, height: viewport.height });
      await expect(page.getByRole('button', { name: 'Search conversation', exact: true })).toBeVisible();
      await expect(page.getByRole('button', { name: 'Toggle session details' })).toBeVisible();
      await expect(page.getByTestId('chat-header-work')).toHaveCount(0);
      await expect(page.getByRole('dialog', { name: 'Conversation information' })).toBeVisible();
      await expect(page.getByTestId('chat-main')).not.toHaveCSS('padding-right', '416px');
      await page.getByTestId('conversation-info-full').click();
      await expect(page.getByTestId('conversation-info-star')).toBeVisible();
      await page.getByTestId('conversation-info-work').click();
      await expect(page.getByTestId('work-view')).toBeVisible();
      await expect(page.getByTestId('work-tab-files')).toBeVisible();
      const screenshotPath = testInfo.outputPath(`chat-work-pwa-${viewport.name}.png`);
      await page.screenshot({ path: screenshotPath, animations: 'disabled' });
      await testInfo.attach(`chat-work-pwa-${viewport.name}`, { path: screenshotPath, contentType: 'image/png' });
    }
    await assertFixtureHealthy(page, fixture);
    await page.setViewportSize({ width: 390, height: 844 });
    await expect(page.getByTestId('work-view')).toBeVisible();
    await expect(page.getByTestId('work-tab-files')).toBeVisible();
    const screenshotPath = testInfo.outputPath('chat-work-mobile-390.png');
    await page.screenshot({ path: screenshotPath, animations: 'disabled' });
    await testInfo.attach('chat-work-mobile-390', { path: screenshotPath, contentType: 'image/png' });
  });

  test('workflow builder presents the phased deterministic workflow', async ({ page }, testInfo) => {
    await login(page);
    const fixture = await installTaskCockpitFixture(page);
    await page.setViewportSize({ width: 1440, height: 1200 });
    await page.goto('/workflows', { waitUntil: 'domcontentloaded' });
    await expect(page.getByRole('heading', { name: 'Workflows' })).toBeVisible();
    await expect(page.getByText('Release review', { exact: true }).first()).toBeVisible();
    await expect(page.getByRole('heading', { name: 'Workflow phases' })).toBeVisible();
    await expect.poll(() => page.locator('input').evaluateAll((inputs) => inputs.map((input) => (input as HTMLInputElement).value))).toEqual(
      expect.arrayContaining(['Prepare', 'Investigate', 'Review'])
    );
    const phaseBuilder = page.getByTestId('workflow-phase-builder');
    await phaseBuilder.scrollIntoViewIfNeeded();
    await expect(phaseBuilder.getByTestId('workflow-phase-prepare')).toBeVisible();
    await expect(phaseBuilder.getByTestId('workflow-phase-investigate')).toBeVisible();
    await expect(phaseBuilder.getByTestId('workflow-phase-review')).toBeVisible();
    await assertFixtureHealthy(page, fixture);
    const screenshotPath = testInfo.outputPath('workflow-builder-phases.png');
    await phaseBuilder.screenshot({ path: screenshotPath, animations: 'disabled' });
    await testInfo.attach('workflow-builder-phases', { path: screenshotPath, contentType: 'image/png' });

    const fetchCard = page.getByTestId('workflow-step-canvas').getByRole('button', { name: /fetch/i }).first();
    await fetchCard.click();
    const deterministicStep = page.getByTestId('workflow-step-inspector');
    const deterministicType = deterministicStep.getByRole('combobox', { name: 'Type' });
    await expect(deterministicType).toHaveValue('tool_call');
    await expect(deterministicStep.getByText('tool call', { exact: true })).toBeVisible();
    await deterministicStep.getByRole('button', { name: 'Advanced' }).click();
    await expect(deterministicStep.getByRole('combobox', { name: 'Type' })).toHaveCount(0);
    await deterministicStep.scrollIntoViewIfNeeded();
    await expect(deterministicStep).toContainText('fetch');
    await expect(deterministicStep).toContainText('Deterministic routing and recovery');
    await expect(deterministicStep.getByText('Tool name', { exact: true })).toHaveCount(0);
    await deterministicStep.getByRole('button', { name: 'Tools' }).click();
    await expect(deterministicStep.getByText('Tool name', { exact: true })).toBeVisible();
    await expect(deterministicStep).not.toContainText('Deterministic routing and recovery');
    const deterministicPath = testInfo.outputPath('workflow-builder-deterministic-step.png');
    await deterministicStep.screenshot({ path: deterministicPath, animations: 'disabled' });
    await testInfo.attach('workflow-builder-deterministic-step', { path: deterministicPath, contentType: 'image/png' });
  });
});

async function clickSheetAction(
  page: import('@playwright/test').Page,
  fixture: Awaited<ReturnType<typeof installTaskCockpitFixture>>,
  status: 'draft' | 'running' | 'paused' | 'completed',
  label: string,
  endpoint: string,
  confirm = false
): Promise<void> {
  fixture.setStatus(status);
  await navigateCockpit(page);
  await page.getByRole('button', { name: 'More task actions' }).click();
  const before = fixture.actionRequests().length;
  await page.getByRole('dialog', { name: 'Task actions' }).getByRole('button', { name: label }).click();
  if (confirm) {
    await page.getByRole('dialog', { name: 'Cancel task?' }).getByRole('button', { name: 'Cancel task' }).click();
  }
  await expect.poll(() => fixture.actionRequests().length).toBe(before + 1);
  const requests = fixture.actionRequests();
  expect(requests[requests.length - 1]).toBe(`POST /api/v1/tasks/${TASK_ID}/${endpoint}`);
}

async function assertContainedInViewport(
  page: import('@playwright/test').Page,
  locator: import('@playwright/test').Locator
): Promise<void> {
  const box = await locator.boundingBox();
  expect(box).not.toBeNull();
  expect(box!.x).toBeGreaterThanOrEqual(0);
  expect(box!.x + box!.width).toBeLessThanOrEqual(await page.evaluate(() => window.innerWidth));
  expect(box!.y).toBeGreaterThanOrEqual(0);
  expect(box!.y + box!.height).toBeLessThanOrEqual(await page.evaluate(() => window.innerHeight));
}

async function assertDescendantsContained(
  page: import('@playwright/test').Page,
  locator: import('@playwright/test').Locator
): Promise<void> {
  const controls = locator.locator('button:visible, a:visible, [data-testid^="task-cockpit-action-"]:visible');
  for (let index = 0; index < await controls.count(); index += 1) {
    await assertContainedInViewport(page, controls.nth(index));
  }
}

async function assertFixtureHealthy(
  page: import('@playwright/test').Page,
  fixture: Awaited<ReturnType<typeof installTaskCockpitFixture>>
): Promise<void> {
  expect(fixture.unmockedRequests()).toEqual([]);
  await expect(page.locator('[data-toast-variant="error"]')).toHaveCount(0);
}

async function navigateCockpit(page: import('@playwright/test').Page): Promise<void> {
  await page.goto(`/tasks/${TASK_ID}`, { waitUntil: 'domcontentloaded' });
  if (
    await page.getByText('This section could not be loaded', { exact: true })
      .isVisible()
      .catch(() => false)
  ) {
    await page.reload({ waitUntil: 'domcontentloaded' });
  }
  await expect(page.getByRole('heading', { name: 'Release safety review' })).toBeVisible();
}
