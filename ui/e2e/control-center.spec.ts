import { expect, test } from '@playwright/test';

import { ADMIN_EMAIL, login } from './helpers';
import { installTaskCockpitFixture, TASK_ID } from './task-cockpit-fixture';

test.describe('Control Center', () => {
  test('contains long issue rows within an iPhone PWA viewport', async ({ page }) => {
    await login(page);
    await installTaskCockpitFixture(page);
    await page.route('**/api/v1/dashboard/issues', async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          generated_at: '2026-08-30T20:00:00Z',
          summary: { total: 1, critical: 1, warning: 0, info: 0, truncated: false },
          issues: [{
            id: 'scheduled-release',
            severity: 'critical',
            kind: 'schedule_failed',
            title: 'Weekly-Cognis-public-GitHub-release-with-an-uninterrupted-identifier-failed-automatically',
            detail: 'Six-consecutive-failures-with-a-very-long-unbroken-diagnostic-reference',
            resource: { type: 'schedule', id: 'scheduled-release', label: 'Weekly release' },
            observed_at: '2026-08-30T20:00:00Z',
            action_url: '/schedules/scheduled-release',
            action_label: 'View schedule',
            dismiss_token: 'scheduled-release-token',
          }],
        }),
      });
    });
    await page.addInitScript(() => {
      Object.defineProperty(window.navigator, 'standalone', { configurable: true, value: true });
      document.documentElement.style.setProperty('--app-safe-area-top', '24px');
    });
    await page.setViewportSize({ width: 390, height: 844 });
    await page.goto('/');

    const row = page.getByTestId('dashboard-issue-scheduled-release');
    await expect(row).toBeVisible();
    const geometry = await row.evaluate((node) => {
      const title = node.querySelector('p');
      const style = title ? getComputedStyle(title) : null;
      return {
        rowWidth: node.getBoundingClientRect().width,
        viewportWidth: window.innerWidth,
        documentWidth: document.documentElement.scrollWidth,
        titleWrap: style?.overflowWrap,
        titleLines: title ? Math.round(title.getBoundingClientRect().height / Number.parseFloat(style?.lineHeight ?? '1')) : 0,
      };
    });
    expect(geometry.rowWidth).toBeLessThanOrEqual(geometry.viewportWidth);
    expect(geometry.documentWidth).toBeLessThanOrEqual(geometry.viewportWidth);
    expect(geometry.titleWrap).toBe('anywhere');
    expect(geometry.titleLines).toBeGreaterThan(1);
    await expect(page.getByTestId('dashboard-issue-dismiss-scheduled-release')).toBeVisible();
    await expect(page.getByTestId('dashboard-issue-action-scheduled-release')).toBeVisible();
  });

  test('resolves approval, credential, and question quick actions without opening the task', async ({ page }) => {
    await login(page);
    await installTaskCockpitFixture(page, { dashboardWorkspaceWindows: true });
    await page.setViewportSize({ width: 1440, height: 900 });
    const source = {
      notification_id: '',
      conversation_id: 'conversation-attention',
      managed_origin_conversation_id: null,
      task_id: 'task-attention',
      step_name: 'review',
      step_run_id: 'run-attention',
      session_id: 'session-attention'
    };
    const summaries = [
      { action_id: 'approval-action', kind: 'escalation', title: 'Tool approval required' },
      { action_id: 'credential-action', kind: 'credential_request', title: 'Credential required' },
      { action_id: 'question-action', kind: 'step_question', title: 'Response required' }
    ].map((action) => ({
      ...action,
      status: 'pending',
      availability: 'actionable',
      source: { ...source, notification_id: action.action_id },
      can_resolve: true,
      has_action_form: true,
      expires_at: null,
      revision: 1,
      convergence_id: `${action.action_id}:1`
    }));
    const task = {
      task_id: 'task-attention',
      title: 'Attention task',
      status: 'running',
      priority: 1,
      agent_id: 'riker',
      workflow_id: null,
      project_id: null,
      source_type: 'manual',
      source_ref: null,
      created_at: '2026-08-24T08:00:00Z',
      started_at: '2026-08-24T08:00:00Z',
      completed_at: null,
      updated_at: '2026-08-24T08:00:00Z',
      result_summary: null,
      attention_type: 'escalation',
      attention_actions: summaries,
      progress_summary: null
    };
    await page.route('**/api/v1/tasks/board**', async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          columns: {
            running: { items: [task], groups: [], cursor: null, has_more: false, total_count: 1 },
            paused: { items: [], groups: [], cursor: null, has_more: false, total_count: 0 },
            ready: { items: [], groups: [], cursor: null, has_more: false, total_count: 0 },
            done: { items: [], groups: [], cursor: null, has_more: false, total_count: 0 }
          }
        })
      });
    });
    const details: Record<string, Record<string, unknown>> = {
      'approval-action': {
        display: {
          message: 'Approve the tool.', tool_name: 'deploy', arguments_display: { target: 'staging' },
          reasoning: null, risk: 'medium', questions: [], required_fields: [], credential_id: null,
          credential_kind: null, credential_label: null, credential_scope: null,
          authorization_url: null, user_code: null, callback_mode: null, executor_name: null
        },
        allowed_actions: [{ action: 'approve', label: 'Approve', intent: 'primary', input: 'note' }]
      },
      'credential-action': {
        display: {
          message: 'Enter the token.', tool_name: null, arguments_display: null, reasoning: null,
          risk: null, questions: [], required_fields: ['token'], credential_id: 'service-token',
          credential_kind: 'token', credential_label: 'Service token', credential_scope: 'user',
          authorization_url: null, user_code: null, callback_mode: null, executor_name: null
        },
        allowed_actions: [{ action: 'approve', label: 'Save and resume', intent: 'primary', input: 'credential' }]
      },
      'question-action': {
        display: {
          message: null, tool_name: null, arguments_display: null, reasoning: null, risk: null,
          questions: [{ id: 'choice', question: 'Continue?', header: null, options: [{ id: 'yes', label: 'Yes', description: null }], multiple: false, allow_custom: false, required: true }],
          required_fields: [], credential_id: null, credential_kind: null, credential_label: null,
          credential_scope: null, authorization_url: null, user_code: null, callback_mode: null,
          executor_name: null
        },
        allowed_actions: [{ action: 'continue', label: 'Send response', intent: 'primary', input: 'structured' }]
      }
    };
    await page.route('**/api/v1/attention-actions/**', async (route) => {
      const pathParts = new URL(route.request().url()).pathname.split('/');
      const actionId = pathParts[pathParts.length - 1] ?? '';
      if (route.request().method() === 'POST') {
        const resolvedId = pathParts[pathParts.length - 2] ?? '';
        await route.fulfill({
          status: 200,
          contentType: 'application/json',
          body: JSON.stringify({ action_id: resolvedId, status: 'resolved', decision: 'approve', revision: 3, convergence_id: `${resolvedId}:3` })
        });
        return;
      }
      const summary = summaries.find((item) => item.action_id === actionId);
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ ...summary, ...details[actionId] })
      });
    });
    let taskDetailRequests = 0;
    page.on('request', (request) => {
      if (new URL(request.url()).pathname === '/api/v1/tasks/task-attention') taskDetailRequests += 1;
    });

    await page.goto('/');
    await page.getByTestId('dashboard-attention-approval-action').click();
    await expect(page.getByTestId('workspace-window-attention:approval-action')).toBeVisible();
    const approvalRequest = page.waitForRequest((request) => (
      request.method() === 'POST'
      && new URL(request.url()).pathname === '/api/v1/attention-actions/approval-action/resolve'
    ));
    const approveButton = page.getByRole('button', { name: 'Approve' });
    await approveButton.click();
    await approvalRequest;
    await expect(page.getByTestId('workspace-window-attention:approval-action')).toHaveCount(0);

    await page.getByTestId('dashboard-attention-credential-action').click();
    await page.getByLabel('Token').fill('secret-token');
    await page.getByRole('button', { name: 'Save and resume' }).click();
    await expect(page.getByTestId('workspace-window-attention:credential-action')).toHaveCount(0);

    await page.getByTestId('dashboard-attention-question-action').click();
    await page.getByLabel('Yes').check();
    await page.getByRole('button', { name: 'Send response' }).click();
    await expect(page.getByTestId('workspace-window-attention:question-action')).toHaveCount(0);
    expect(taskDetailRequests).toBe(0);

    await page.setViewportSize({ width: 390, height: 844 });
    let dismissMutationRequests = 0;
    page.on('request', (request) => {
      if (
        request.method() === 'POST'
        && new URL(request.url()).pathname.includes('/api/v1/attention-actions/')
      ) dismissMutationRequests += 1;
    });
    const mobileAction = page.getByTestId('dashboard-attention-approval-action');
    await mobileAction.click();
    const closeButton = page.getByTestId('attention-dialog-close');
    await expect(closeButton).toBeVisible();
    const closeBox = await closeButton.boundingBox();
    expect(closeBox?.width).toBeGreaterThanOrEqual(44);
    expect(closeBox?.height).toBeGreaterThanOrEqual(44);
    await closeButton.click();
    await expect(closeButton).toHaveCount(0);
    await expect(mobileAction).toBeFocused();
    expect(dismissMutationRequests).toBe(0);
    await expect(page.locator('[data-app-content="true"]')).not.toHaveAttribute('data-overlay-scroll-locked');
    expect(await page.evaluate(() => history.state?.cognisAttentionActionId ?? null)).toBeNull();

    await mobileAction.click();
    await page.keyboard.press('Escape');
    await expect(closeButton).toHaveCount(0);
    await expect(mobileAction).toBeFocused();
    expect(dismissMutationRequests).toBe(0);

    await mobileAction.click();
    await page.goBack();
    await expect(closeButton).toHaveCount(0);
    await expect(page).toHaveURL('/');
    await expect(mobileAction).toBeFocused();
    expect(dismissMutationRequests).toBe(0);

    await mobileAction.click();
    await expect(closeButton).toBeVisible();
    await closeButton.click();
    await expect(mobileAction).toBeFocused();
  });

  test.use({ serviceWorkers: 'block' });

  test('searches and paginates Recent tasks and Conversations on the server', async ({ page }) => {
    const taskRequests: URL[] = [];
    const conversationRequests: URL[] = [];
    const timestamp = '2026-08-31T12:00:00Z';
    const task = (taskId: string, title: string) => ({
      task_id: taskId,
      title,
      status: 'completed',
      priority: 1,
      agent_id: 'riker',
      workflow_id: null,
      project_id: null,
      source_type: 'manual',
      source_ref: null,
      created_at: timestamp,
      started_at: timestamp,
      completed_at: timestamp,
      updated_at: timestamp,
      result_summary: null,
      attention_type: null,
      progress_summary: null,
    });
    const conversation = (conversationId: string, title: string) => ({
      conversation_id: conversationId,
      user_email: ADMIN_EMAIL,
      agent_id: 'riker',
      agent_profile_id: null,
      project_id: null,
      title,
      title_source: 'user',
      context: {
        type: 'web',
        ref: `web:topic:${conversationId}`,
        platform_data: { kind: 'topic' },
        memory_labels: {},
      },
      active_session_id: null,
      active_session_status: null,
      active_session_completion_reason: null,
      active_turn_chat_mode: null,
      active_turn_chat_mode_source: null,
      pending_notification_types: [],
      starred_at: null,
      status: 'active',
      last_message_at: timestamp,
      last_read_at: timestamp,
      has_unread: false,
      has_active_turn: false,
      managed_agent: null,
      root_controller_conversation_id: null,
      created_at: timestamp,
      updated_at: timestamp,
      conversation_state: null,
    });
    const taskColumns = (
      items: ReturnType<typeof task>[],
      cursor: string | null,
      hasMore: boolean,
    ) => ({
      columns: {
        draft: { items: [], groups: [], cursor: null, has_more: false, total_count: 0 },
        queued: { items: [], groups: [], cursor: null, has_more: false, total_count: 0 },
        running: { items: [], groups: [], cursor: null, has_more: false, total_count: 0 },
        paused: { items: [], groups: [], cursor: null, has_more: false, total_count: 0 },
        done: { items, groups: [], cursor, has_more: hasMore, total_count: items.length + (hasMore ? 1 : 0) },
      },
    });
    const initialTasks = Array.from({ length: 5 }, (_, index) => (
      task(`recent-task-${index + 1}`, `Recent task ${index + 1}`)
    ));
    const initialConversations = Array.from({ length: 20 }, (_, index) => (
      conversation(`recent-conversation-${index + 1}`, `Recent conversation ${index + 1}`)
    ));

    await login(page);
    await page.route(/\/api\/v1\/projects(?:\?.*)?$/, async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify([{
          project_id: 'project-search',
          name: 'Search project',
          description: null,
          source_path: null,
          remote_url: null,
          status: 'active',
          created_at: timestamp,
          updated_at: timestamp,
        }]),
      });
    });
    await page.route(/\/api\/v1\/tasks\/board(?:\/done)?(?:\?.*)?$/, async (route) => {
      const url = new URL(route.request().url());
      taskRequests.push(url);
      const query = url.searchParams.get('q');
      const projectId = url.searchParams.get('project_id');
      const cursor = url.searchParams.get('cursor');
      let body;
      if (query === 'needle') {
        body = taskColumns(
          [task(projectId ? 'project-task-result' : 'search-task-result', 'Server-selected task')],
          null,
          false,
        );
      } else if (cursor === 'task-page-2') {
        body = {
          items: [task('recent-task-6', 'Recent task 6')],
          groups: [],
          cursor: 'task-page-2',
          has_more: true,
          total_count: 6,
        };
      } else {
        body = taskColumns(initialTasks, 'task-page-2', true);
      }
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(body) });
    });
    await page.route(/\/api\/v1\/conversations(?:\?.*)?$/, async (route) => {
      if (route.request().method() !== 'GET') {
        await route.continue();
        return;
      }
      const url = new URL(route.request().url());
      conversationRequests.push(url);
      const query = url.searchParams.get('q');
      const projectId = url.searchParams.get('project_id');
      const cursor = url.searchParams.get('cursor');
      const items = query === 'needle'
        ? [conversation(
            projectId ? 'project-conversation-result' : 'search-conversation-result',
            'Backend result without matching text',
          )]
        : cursor === 'conversation-page-2'
          ? [conversation('recent-conversation-21', 'Recent conversation 21')]
          : initialConversations;
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          items,
          cursor: query || cursor ? null : 'conversation-page-2',
          has_more: !query && !cursor,
        }),
      });
    });

    await page.setViewportSize({ width: 1440, height: 800 });
    await page.goto('/');
    const taskList = page.getByTestId('dashboard-tasks-recent-list');
    const conversationList = page.getByTestId('dashboard-conversations-recent-list');
    await expect(page.getByTestId('dashboard-task-row-recent-task-1')).toBeVisible();
    await expect(page.getByTestId('dashboard-conversation-row-recent-conversation-1')).toBeVisible();

    const initialConversationRequests = conversationRequests.length;
    await taskList.hover();
    await page.mouse.wheel(0, 10_000);
    await expect(page.getByTestId('dashboard-task-row-recent-task-6')).toBeVisible();
    await expect.poll(() => taskRequests.filter((url) => url.searchParams.get('cursor') === 'task-page-2').length).toBe(1);
    await expect(page.getByTestId('dashboard-tasks-recent-sentinel')).toHaveCount(0);
    expect(conversationRequests.length).toBe(initialConversationRequests);
    const taskRequestsAfterPagination = taskRequests.length;
    await page.mouse.wheel(0, 10_000);
    await page.waitForTimeout(100);
    expect(taskRequests.filter((url) => url.searchParams.get('cursor') === 'task-page-2')).toHaveLength(1);

    await conversationList.hover();
    await page.mouse.wheel(0, 10_000);
    await expect(page.getByTestId('dashboard-conversation-row-recent-conversation-21')).toBeVisible();
    await expect.poll(() => conversationRequests.filter(
      (url) => url.searchParams.get('cursor') === 'conversation-page-2',
    ).length).toBe(1);
    await expect(page.getByTestId('dashboard-conversations-recent-sentinel')).toHaveCount(0);
    expect(taskRequests.length).toBe(taskRequestsAfterPagination);

    await page.getByLabel('Search tasks and conversations').fill('  needle  ');
    await expect(page.getByTestId('dashboard-task-row-search-task-result')).toBeVisible();
    await expect.poll(() => conversationRequests.map((url) => url.searchParams.get('q'))).toContain('needle');
    await expect(page.getByTestId('dashboard-conversation-row-search-conversation-result')).toBeVisible();
    await expect(page.getByTestId('dashboard-task-row-recent-task-6')).toHaveCount(0);
    await expect(page.getByTestId('dashboard-conversation-row-recent-conversation-21')).toHaveCount(0);
    await expect.poll(() => taskRequests.some((url) => (
      url.searchParams.get('q') === 'needle' && !url.searchParams.has('cursor')
    ))).toBe(true);
    await expect.poll(() => conversationRequests.some((url) => (
      url.searchParams.get('q') === 'needle' && !url.searchParams.has('cursor')
    ))).toBe(true);

    await page.getByLabel('Filter dashboard by project').selectOption('project-search');
    await expect(page.getByTestId('dashboard-task-row-project-task-result')).toBeVisible();
    await expect(page.getByTestId('dashboard-conversation-row-project-conversation-result')).toBeVisible();
    await expect.poll(() => taskRequests.some((url) => (
      url.searchParams.get('q') === 'needle'
      && url.searchParams.get('project_id') === 'project-search'
      && !url.searchParams.has('cursor')
    ))).toBe(true);
    await expect.poll(() => conversationRequests.some((url) => (
      url.searchParams.get('q') === 'needle'
      && url.searchParams.get('project_id') === 'project-search'
      && !url.searchParams.has('cursor')
    ))).toBe(true);

    await expect(page.getByRole('button', { name: /load more/i })).toHaveCount(0);
    const geometry = await page.evaluate(() => {
      const controlCenter = document.querySelector<HTMLElement>('[data-testid="control-center"]');
      const taskBody = document.querySelector<HTMLElement>('[data-testid="dashboard-tasks-recent-list"]');
      const conversationBody = document.querySelector<HTMLElement>(
        '[data-testid="dashboard-conversations-recent-list"]',
      );
      if (!controlCenter || !taskBody || !conversationBody) throw new Error('Dashboard lists are missing');
      return {
        pageOverflow: document.documentElement.scrollHeight - document.documentElement.clientHeight,
        dashboardHeight: controlCenter.getBoundingClientRect().height,
        viewportHeight: window.innerHeight,
        taskOverflowY: getComputedStyle(taskBody).overflowY,
        conversationOverflowY: getComputedStyle(conversationBody).overflowY,
      };
    });
    expect(geometry.pageOverflow).toBeLessThanOrEqual(1);
    expect(geometry.dashboardHeight).toBeLessThanOrEqual(geometry.viewportHeight);
    expect(geometry.taskOverflowY).toBe('auto');
    expect(geometry.conversationOverflowY).toBe('auto');
  });

  test('keeps both desktop columns inside the app content viewport', async ({ page }, testInfo) => {
    await login(page);
    await page.route('**/api/v1/tasks/board**', async (route) => {
      const item = {
        task_id: 'overflow-task',
        title: 'A deliberately long running task title that must truncate inside the Tasks column without widening the dashboard grid',
        status: 'running',
        priority: 5,
        agent_id: 'riker',
        workflow_id: null,
        project_id: null,
        source_type: 'manual',
        source_ref: null,
        created_at: '2026-08-24T08:00:00Z',
        started_at: '2026-08-24T08:01:00Z',
        completed_at: null,
        updated_at: '2026-08-24T08:02:00Z',
        result_summary: null,
        attention_type: null,
        progress_summary: null
      };
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          columns: {
            draft: { items: [], groups: [], cursor: null, has_more: false, total_count: 0 },
            queued: { items: [], groups: [], cursor: null, has_more: false, total_count: 0 },
            running: { items: [item], groups: [], cursor: null, has_more: false, total_count: 1 },
            paused: { items: [], groups: [], cursor: null, has_more: false, total_count: 0 },
            done: { items: [], groups: [], cursor: null, has_more: false, total_count: 0 }
          }
        })
      });
    });
    const widths = [1210, 1280, 1440, 1920, 3024];

    for (const width of widths) {
      await page.setViewportSize({ width, height: width === 1210 ? 834 : 1200 });
      await page.goto('/');
      await expect(page.getByTestId('dashboard-tasks-section')).toBeVisible();
      await expect(page.getByTestId('dashboard-conversations-section')).toBeVisible();

      const layout = await page.evaluate(() => {
        const app = document.querySelector<HTMLElement>('[data-app-content="true"]');
        const tasks = document.querySelector<HTMLElement>('[data-testid="dashboard-tasks-section"]');
        const conversations = document.querySelector<HTMLElement>('[data-testid="dashboard-conversations-section"]');
        const rows = [...document.querySelectorAll<HTMLElement>(
          '[data-testid^="dashboard-task-row-"], [data-testid^="dashboard-conversation-row-"]'
        )];
        if (!app || !tasks || !conversations) throw new Error('Dashboard layout is incomplete');
        const appBox = app.getBoundingClientRect();
        const tasksBox = tasks.getBoundingClientRect();
        const conversationsBox = conversations.getBoundingClientRect();
        return {
          documentOverflow: document.documentElement.scrollWidth - document.documentElement.clientWidth,
          appOverflow: app.scrollWidth - app.clientWidth,
          appLeft: appBox.left,
          appRight: appBox.right,
          tasksLeft: tasksBox.left,
          tasksRight: tasksBox.right,
          tasksWidth: tasksBox.width,
          conversationsLeft: conversationsBox.left,
          conversationsRight: conversationsBox.right,
          conversationsWidth: conversationsBox.width,
          rowCount: rows.length,
          rowOverflow: rows.map((row) => row.scrollWidth - row.clientWidth)
        };
      });

      expect(layout.documentOverflow).toBeLessThanOrEqual(1);
      expect(layout.appOverflow).toBeLessThanOrEqual(1);
      expect(layout.tasksLeft).toBeGreaterThanOrEqual(layout.appLeft - 1);
      expect(layout.conversationsRight).toBeLessThanOrEqual(layout.appRight + 1);
      expect(layout.conversationsLeft).toBeGreaterThan(layout.tasksRight);
      expect(layout.tasksWidth / layout.conversationsWidth).toBeGreaterThan(1.3);
      expect(layout.tasksWidth / layout.conversationsWidth).toBeLessThan(1.5);
      expect(layout.rowCount).toBeGreaterThan(0);
      expect(Math.max(0, ...layout.rowOverflow)).toBeLessThanOrEqual(1);

      if (width === 3024) {
        const screenshotPath = testInfo.outputPath('control-center-wide-no-overflow.png');
        await page.screenshot({ path: screenshotPath, fullPage: true, animations: 'disabled' });
        await testInfo.attach('control-center-wide-no-overflow', {
          path: screenshotPath,
          contentType: 'image/png'
        });
      }
    }
  });

  test('keeps iPad landscape desktop structure with compact touch chrome', async ({ browser }) => {
    const context = await browser.newContext({
      viewport: { width: 1210, height: 834 },
      deviceScaleFactor: 2,
      hasTouch: true,
      isMobile: true,
      serviceWorkers: 'block',
      userAgent: 'Mozilla/5.0 (iPad; CPU OS 18_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1',
    });
    const page = await context.newPage();
    await page.addInitScript(() => {
      Object.defineProperty(window.navigator, 'standalone', { configurable: true, value: true });
      document.documentElement.style.setProperty('--app-safe-area-top', '24px');
    });

    try {
      await login(page);
      await installTaskCockpitFixture(page, { dashboardWorkspaceWindows: true });
      await page.goto('/');
      await expect(page.getByTestId('dashboard-tasks-section')).toBeVisible();
      await expect(page.getByTestId('dashboard-conversations-section')).toBeVisible();
      const viewportGeometry = await page.evaluate(() => {
        const app = document.querySelector<HTMLElement>('[data-app-content="true"]');
        if (!app) throw new Error('App content is missing');
        const box = app.getBoundingClientRect();
        return {
          top: box.top,
          bottom: box.bottom,
          innerHeight: window.innerHeight,
          visualBottom: (window.visualViewport?.offsetTop ?? 0)
            + (window.visualViewport?.height ?? window.innerHeight),
          pageOverflow: document.documentElement.scrollHeight - document.documentElement.clientHeight,
          safeTop: Number.parseFloat(
            getComputedStyle(document.documentElement).getPropertyValue('--app-safe-area-top'),
          ) || 0,
        };
      });
      expect(viewportGeometry.top).toBeGreaterThanOrEqual(viewportGeometry.safeTop);
      expect(viewportGeometry.bottom).toBeLessThanOrEqual(
        Math.max(viewportGeometry.innerHeight, viewportGeometry.visualBottom) + 1,
      );
      expect(viewportGeometry.pageOverflow).toBeLessThanOrEqual(1);

      const [tasksBox, conversationsBox] = await Promise.all([
        page.getByTestId('dashboard-tasks-section').boundingBox(),
        page.getByTestId('dashboard-conversations-section').boundingBox(),
      ]);
      expect(tasksBox?.x).toBeLessThan(conversationsBox?.x ?? 0);

      await page.getByTestId(`dashboard-task-row-${TASK_ID}`).click();
      const workspaceWindow = page.getByTestId(`workspace-window-task:${TASK_ID}`);
      await expect(workspaceWindow).toBeVisible();
      const workspaceHeader = workspaceWindow.locator('[data-testid^="workspace-window-drag-"]');
      const workspaceHeaderBox = await workspaceHeader.boundingBox();
      expect(workspaceHeaderBox?.height).toBeLessThanOrEqual(41);
      await expect(workspaceWindow.getByLabel('Close')).toHaveCSS('width', '40px');
      const workspaceBox = await workspaceWindow.boundingBox();
      const workspaceBottom = (workspaceBox?.y ?? 0) + (workspaceBox?.height ?? 0);
      const visualBottom = await page.evaluate(() => (
        (window.visualViewport?.offsetTop ?? 0)
        + (window.visualViewport?.height ?? window.innerHeight)
      ));
      expect(workspaceBottom).toBeLessThanOrEqual(visualBottom + 1);
      expect(workspaceBox?.y ?? 0).toBeGreaterThanOrEqual(viewportGeometry.top);

      await page.goto('/chat/conv-task-chat');
      const chatHeader = page.getByTestId('chat-header');
      await expect(chatHeader).toBeVisible();
      const chatHeaderBox = await chatHeader.boundingBox();
      const chatContentTop = await page.locator('[data-app-content="true"]').evaluate(
        (element) => element.getBoundingClientRect().top,
      );
      expect(chatHeaderBox?.height).toBeLessThanOrEqual(46);
      expect(chatHeaderBox?.y).toBeGreaterThanOrEqual(chatContentTop);
      await expect.poll(() => page.evaluate(() => (
        document.documentElement.scrollHeight - document.documentElement.clientHeight
      ))).toBeLessThanOrEqual(1);
      await expect(page.getByRole('button', { name: 'Search conversation', exact: true })).toHaveCSS('width', '36px');
      await page.getByTestId('chat-header-info').click();
      const inspector = page.getByTestId('conversation-info-drawer');
      await expect(inspector).toBeVisible();
      expect(await inspector.evaluate((element) => element.tagName)).toBe('ASIDE');
      const inspectorBox = await inspector.boundingBox();
      expect(inspectorBox?.y).toBeGreaterThanOrEqual(chatContentTop);
      expect((inspectorBox?.y ?? 0) + (inspectorBox?.height ?? 0))
        .toBeLessThanOrEqual(visualBottom + 1);
      expect(inspectorBox?.width).toBeGreaterThanOrEqual(320);
      expect(inspectorBox?.width).toBeLessThanOrEqual(352);
      await expect(page.getByTestId('conversation-info-resizer')).toHaveCSS('width', '44px');
      await expect(page.getByTestId('chat-sidebar-resizer')).toHaveCSS('width', '44px');
    } finally {
      await context.close();
    }
  });

  test('creates a dashboard chat with the selected agent and profile and preserves failed input', async ({ page }) => {
    const primaryAgents = [
      {
        agent_id: 'agent-default',
        name: 'Default agent',
        display_name: 'Default agent',
        agent_type: 'primary',
        status: 'active',
        avatar_url: null,
        default_agent_profile_id: 'fast',
        agent_profiles: {
          fast: { profile_id: 'fast', description: 'Fast', enabled: true, agent_switchable: true }
        }
      },
      {
        agent_id: 'agent-selected',
        name: 'Selected agent',
        display_name: 'Selected agent',
        agent_type: 'primary',
        status: 'active',
        avatar_url: null,
        default_agent_profile_id: 'fast',
        agent_profiles: {
          fast: { profile_id: 'fast', description: 'Fast', enabled: true, agent_switchable: true },
          quality: { profile_id: 'quality', description: 'Quality', enabled: true, agent_switchable: true }
        }
      }
    ];
    const createdConversation = {
      conversation_id: 'conv-new-profile',
      user_email: 'admin@cognis-e2e.localdev.me',
      agent_id: 'agent-selected',
      agent_profile_id: 'quality',
      project_id: null,
      title: 'Selected profile conversation',
      title_source: 'manual',
      context: { type: 'web', ref: null, platform_data: {}, memory_labels: {} },
      active_session_id: null,
      status: 'active',
      created_at: '2026-08-25T22:00:00Z',
      updated_at: '2026-08-25T22:00:00Z',
      last_message_at: null,
      has_unread: false,
      has_active_turn: false,
      active_session_status: null,
      active_session_completion_reason: null,
      pending_notification_types: []
    };
    const createPayloads: Array<Record<string, unknown>> = [];

    await login(page);
    expect((await page.request.put('/api/v1/user-preferences', {
      data: {
        display: { theme: 'dark', language: 'en', dashboard_workspace_windows: true },
        chat: {
          show_thinking_blocks: false,
          group_tool_calls: true,
          keep_assistant_messages_separate: false,
          show_internal_tool_calls: false
        }
      }
    })).ok()).toBe(true);
    await page.route(/\/api\/v1\/agents(?:\?.*)?$/, async (route) => {
      if (route.request().method() !== 'GET') {
        await route.continue();
        return;
      }
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ items: primaryAgents, cursor: null, has_more: false })
      });
    });
    await page.route(/\/api\/v1\/conversations$/, async (route) => {
      if (route.request().method() !== 'POST') {
        await route.continue();
        return;
      }
      createPayloads.push(route.request().postDataJSON() as Record<string, unknown>);
      if (createPayloads.length === 1) {
        await route.fulfill({
          status: 503,
          contentType: 'application/json',
          body: JSON.stringify({ detail: 'Temporary create failure' })
        });
        return;
      }
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(createdConversation)
      });
    });
    await page.route('**/api/v1/conversations/conv-new-profile', async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(createdConversation)
      });
    });

    await page.setViewportSize({ width: 1440, height: 1000 });
    await page.goto('/');
    await page.getByTestId('dashboard-conversations-new').click();
    const dialog = page.getByRole('dialog', { name: 'Start a new chat' });
    const agentSelect = dialog.getByLabel('Primary agent');
    const profileSelect = dialog.getByLabel('Agent profile');
    await agentSelect.selectOption('agent-selected');
    await profileSelect.selectOption('quality');
    await dialog.getByRole('button', { name: 'Create conversation' }).click();

    await expect(dialog.getByText('Temporary create failure')).toBeVisible();
    await expect(agentSelect).toHaveValue('agent-selected');
    await expect(profileSelect).toHaveValue('quality');

    await dialog.getByRole('button', { name: 'Create conversation' }).click();
    await expect(dialog).toHaveCount(0);
    await expect(page.getByTestId('workspace-window-conversation:conv-new-profile')).toBeVisible();
    expect(createPayloads).toEqual([
      {
        agent_id: 'agent-selected',
        agent_profile_id: 'quality',
        context: {
          type: 'web',
          ref: null,
          platform_data: {},
          memory_labels: {}
        }
      },
      {
        agent_id: 'agent-selected',
        agent_profile_id: 'quality',
        context: {
          type: 'web',
          ref: null,
          platform_data: {},
          memory_labels: {}
        }
      }
    ]);
  });

  test('renders bounded sections and opens entity rows without owner-wide activity scans', async ({ page }, testInfo) => {
    const ownerWideActivityRequests: string[] = [];
    page.on('request', (request) => {
      if (request.url().includes('/api/v1/work/activities')) {
        ownerWideActivityRequests.push(request.url());
      }
    });

    await login(page);
    expect((await page.request.put('/api/v1/user-preferences', {
      data: {
        display: { theme: 'dark', language: 'en', dashboard_workspace_windows: false },
        chat: {
          show_thinking_blocks: false,
          group_tool_calls: true,
          keep_assistant_messages_separate: false,
          show_internal_tool_calls: false
        }
      }
    })).ok()).toBe(true);
    await page.setViewportSize({ width: 1440, height: 1000 });
    await page.goto('/');

    await expect(page.getByTestId('control-center')).toBeVisible();
    await expect(page.getByTestId('dashboard-issues-healthy')).toHaveCount(0);
    await expect(page.getByTestId('dashboard-tasks-section')).toBeVisible();
    await expect(page.getByTestId('dashboard-conversations-section')).toBeVisible();
    await expect(page.getByTestId('sidebar-logo-link')).toHaveAttribute('href', '/');
    await expect(page.getByRole('link', { name: 'Work', exact: true })).toHaveCount(0);
    const tasksBox = await page.getByTestId('dashboard-tasks-section').boundingBox();
    const conversationsBox = await page.getByTestId('dashboard-conversations-section').boundingBox();
    expect(tasksBox?.x).toBeLessThan(conversationsBox?.x ?? 0);
    await expect(page.getByTestId('dashboard-conversations-agents')).toBeVisible();
    await expect(page.getByTestId('control-center-agents-footer')).toHaveCount(0);

    const runningRows = page.getByTestId('dashboard-tasks-running').locator('[data-testid^="dashboard-task-row-"]');
    if (await runningRows.count()) {
      await expect(runningRows.first().getByTestId('workstream-execution-status')).toBeVisible();
      const todoProgress = runningRows.first().getByTestId('workstream-todo-progress');
      if (await todoProgress.count()) await expect(todoProgress).toBeVisible();
      const diff = runningRows.first().getByTestId('diff-stat');
      if (await diff.count()) await expect(diff).toBeVisible();
    }

    const conversationRows = page.locator('[data-testid^="dashboard-conversation-row-"]:not([data-testid*="-link-"])');
    await expect(conversationRows.first()).toBeVisible();
    await conversationRows.first().click();
    await expect(page.getByTestId('dashboard-entity-modal')).toBeVisible();
    await expect(page.getByTestId('dashboard-conversation-chat')).toBeVisible();
    const topicChat = page.getByTestId('dashboard-conversation-chat').getByTestId('compact-conversation-chat');
    await expect(topicChat).toHaveAttribute('data-embedded', 'true');
    await expect(topicChat).toHaveAttribute('data-auto-tail', 'following');
    const topicViewport = topicChat.getByTestId('scoped-timeline-viewport');
    await expect.poll(() => topicViewport.evaluate((node) => {
      const element = node as HTMLElement;
      return Math.round(element.scrollHeight - element.scrollTop - element.clientHeight);
    })).toBeLessThanOrEqual(2);
    const inspector = page.locator(
      '[data-testid^="dashboard-conversation-inspector-"][data-testid$="-desktop"]'
    );
    await expect(inspector).toBeVisible();
    await expect(inspector.getByRole('tab', { name: 'Overview' })).toHaveAttribute('aria-selected', 'true');
    await inspector.getByRole('tab', { name: 'Work' }).click();
    await expect(inspector.getByRole('tab', { name: 'Work' })).toHaveAttribute('aria-selected', 'true');
    await inspector.getByRole('tab', { name: 'Session' }).click();
    await expect(inspector.getByRole('tab', { name: 'Session' })).toHaveAttribute('aria-selected', 'true');
    await expect(
      page.getByTestId('dashboard-conversation-chat').getByText('Loading timeline…')
    ).toBeHidden({ timeout: 30_000 });
    await expect(page.getByTestId('dashboard-entity-modal').locator('.animate-pulse')).toHaveCount(0, {
      timeout: 30_000
    });
    const modalScreenshotPath = testInfo.outputPath('control-center-modal-desktop.png');
    await page.screenshot({ path: modalScreenshotPath, fullPage: true, animations: 'disabled' });
    await testInfo.attach('control-center-modal-desktop', {
      path: modalScreenshotPath,
      contentType: 'image/png'
    });
    await page.getByTestId('dashboard-entity-modal-close').click();

    expect(ownerWideActivityRequests).toEqual([]);
    const screenshotPath = testInfo.outputPath('control-center-desktop.png');
    await page.screenshot({ path: screenshotPath, fullPage: true, animations: 'disabled' });
    await testInfo.attach('control-center-desktop', { path: screenshotPath, contentType: 'image/png' });

    const issueActions = page.locator('[data-testid^="dashboard-issue-action-"]');
    if (await issueActions.count()) {
      await issueActions.first().click();
      await expect(page).toHaveURL(/\/settings\?tab=(executors|tools)$/);
    }
  });

  test('places unread and attention state only on canonical conversation avatars', async ({ page }) => {
    await login(page);
    const now = '2026-08-24T12:00:00Z';
    const topic = (conversationId: string, overrides: Record<string, unknown>) => ({
      conversation_id: conversationId,
      user_email: 'admin@cognis-e2e.localdev.me',
      agent_id: 'riker',
      agent_profile_id: null,
      project_id: null,
      title: conversationId,
      title_source: 'user',
      context: {
        type: 'web',
        ref: `web:topic:${conversationId}`,
        platform_data: { kind: 'topic' },
        memory_labels: {}
      },
      active_session_id: `session-${conversationId}`,
      active_session_status: 'active',
      active_session_completion_reason: null,
      active_turn_chat_mode: null,
      active_turn_chat_mode_source: null,
      pending_notification_types: [],
      starred_at: null,
      status: 'active',
      last_message_at: now,
      last_read_at: null,
      has_unread: false,
      has_active_turn: false,
      managed_agent: null,
      root_controller_conversation_id: null,
      created_at: now,
      updated_at: now,
      conversation_state: null,
      ...overrides
    });
    await page.route('**/api/v1/conversations?**', async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          items: [
            topic('active-unread', { has_active_turn: true, has_unread: true }),
            topic('waiting-attention', { has_unread: true, pending_notification_types: ['gate'] }),
            topic('recent-unread', { has_unread: true })
          ],
          cursor: null,
          has_more: false
        })
      });
    });
    await page.goto('/');

    const active = page.getByTestId('dashboard-conversation-row-active-unread');
    const waiting = page.getByTestId('dashboard-conversation-row-waiting-attention');
    const recent = page.getByTestId('dashboard-conversation-row-recent-unread');
    await expect(active.getByTestId('activity-avatar-orbit')).toBeVisible();
    await expect(active.getByTestId('activity-avatar-unread')).toHaveCount(0);
    await expect(waiting.getByTestId('activity-avatar-attention')).toBeVisible();
    await expect(recent.getByTestId('activity-avatar-unread')).toBeVisible();
    await expect(page.getByTestId('dashboard-conversations-section').getByText('Unread', { exact: true })).toHaveCount(0);
    await expect(page.getByTestId('dashboard-conversations-section').locator('[aria-label="Unread"]')).toHaveCount(0);
  });

  test('reconciles attention actions on a paginated conversation tail without duplicates', async ({ page }) => {
    await page.addInitScript(() => {
      const NativeWebSocket = window.WebSocket;
      const sockets: WebSocket[] = [];
      Object.defineProperty(window, '__controlCenterSockets', { value: sockets });
      class InspectableWebSocket extends NativeWebSocket {
        constructor(url: string | URL, protocols?: string | string[]) {
          super(url, protocols);
          sockets.push(this);
        }
      }
      window.WebSocket = InspectableWebSocket;
    });
    const now = '2026-08-24T12:00:00Z';
    const topic = (conversationId: string, attentionActions: unknown[] = []) => ({
      conversation_id: conversationId,
      user_email: ADMIN_EMAIL,
      agent_id: 'riker',
      agent_profile_id: null,
      project_id: null,
      title: conversationId,
      title_source: 'user',
      context: {
        type: 'web',
        ref: `web:topic:${conversationId}`,
        platform_data: { kind: 'topic' },
        memory_labels: {},
      },
      active_session_id: null,
      active_session_status: null,
      active_session_completion_reason: null,
      active_turn_chat_mode: null,
      active_turn_chat_mode_source: null,
      pending_notification_types: attentionActions.length ? ['escalation'] : [],
      attention_actions: attentionActions,
      starred_at: null,
      status: 'active',
      last_message_at: now,
      last_read_at: now,
      has_unread: false,
      has_active_turn: false,
      managed_agent: null,
      root_controller_conversation_id: null,
      created_at: now,
      updated_at: now,
      conversation_state: null,
    });
    const summary = (revision: number) => ({
      action_id: 'tail-action',
      kind: 'escalation',
      status: 'pending',
      availability: 'actionable',
      title: 'Tool approval required',
      source: {
        notification_id: 'tail-action',
        conversation_id: 'conversation-tail',
        managed_origin_conversation_id: null,
        task_id: null,
        step_name: null,
        step_run_id: null,
        session_id: null,
      },
      can_resolve: true,
      has_action_form: true,
      expires_at: null,
      revision,
      convergence_id: `tail-action:${revision}`,
    });
    let detailActions: unknown[] = [];
    const detailQueries: URL[] = [];
    await page.route('**/api/v1/conversations?**', async (route) => {
      const url = new URL(route.request().url());
      const cursor = url.searchParams.get('cursor');
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(cursor === 'conversation-tail-page'
          ? {
              items: [topic('conversation-tail')],
              cursor: null,
              has_more: false,
            }
          : {
              items: [topic('conversation-head')],
              cursor: 'conversation-tail-page',
              has_more: true,
            }),
      });
    });
    await page.route('**/api/v1/conversations/conversation-tail?**', async (route) => {
      const url = new URL(route.request().url());
      detailQueries.push(url);
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(topic('conversation-tail', detailActions)),
      });
    });

    await login(page);
    await expect(page.getByTestId('dashboard-conversation-row-conversation-tail')).toBeVisible();
    await expect.poll(() => page.evaluate(() => (
      ((window as typeof window & { __controlCenterSockets?: WebSocket[] })
        .__controlCenterSockets?.length ?? 0)
    ))).toBeGreaterThan(0);
    const invalidate = () => page.evaluate(() => {
      const sockets = (window as typeof window & {
        __controlCenterSockets?: WebSocket[];
      }).__controlCenterSockets ?? [];
      sockets.forEach((socket) => socket.dispatchEvent(new MessageEvent('message', {
        data: JSON.stringify({
          type: 'scope_invalidated',
          reason: 'notification_state_changed',
          conversation_id: 'conversation-tail',
        }),
      })));
    });

    detailActions = [summary(2)];
    await invalidate();
    await expect(page.getByTestId('dashboard-attention-tail-action')).toBeVisible();
    expect(
      detailQueries[detailQueries.length - 1]?.searchParams.get('include_attention_actions'),
    ).toBe('true');
    await expect(page.getByTestId('dashboard-conversation-row-conversation-tail')).toHaveCount(1);

    detailActions = [];
    await invalidate();
    await expect(page.getByTestId('dashboard-attention-tail-action')).toHaveCount(0);
    await expect(page.getByTestId('dashboard-conversation-row-conversation-tail')).toHaveCount(1);

    detailActions = [summary(1)];
    await invalidate();
    await expect(page.getByTestId('dashboard-attention-tail-action')).toHaveCount(0);
    await expect(page.getByTestId('dashboard-conversation-row-conversation-tail')).toHaveCount(1);
  });

  test('uses the mobile section order and entity Chat/Activity tabs', async ({ page }, testInfo) => {
    await page.addInitScript(() => {
      const NativeWebSocket = window.WebSocket;
      const sockets: WebSocket[] = [];
      Object.defineProperty(window, '__controlCenterSockets', { value: sockets });
      class InspectableWebSocket extends NativeWebSocket {
        constructor(url: string | URL, protocols?: string | string[]) {
          super(url, protocols);
          sockets.push(this);
        }
      }
      window.WebSocket = InspectableWebSocket;
    });
    await login(page);
    let timelineBackfillRequests = 0;
    let markReadRequests = 0;
    const mobileTimeline = Array.from({ length: 12 }, (_, index) => {
      const sequence = index + 1;
      const role = sequence % 2 === 0 ? 'assistant' : 'user';
      return {
        id: `message:mobile-control-center-${sequence}`,
        kind: 'message',
        sort_key: `0000:${String(sequence).padStart(15, '0')}:000000:02:000000000`,
        source_refs: [{ store: 'intaris', session_id: 'sess-mobile-control-center', seq: sequence, event_type: `${role}_message` }],
        stable: true,
        role,
        content: role === 'user'
          ? `Mobile timeline question ${Math.ceil(sequence / 2)}`
          : `Mobile timeline answer ${sequence / 2}: deterministic content is loaded at the newest event.`,
        message_id: `msg-mobile-control-center-${sequence}`,
        attachments: [],
        partial: false
      };
    });
    await page.route('**/api/v1/chat/v2/conversations/*/snapshot', async (route) => {
      const url = new URL(route.request().url());
      const conversationId = url.pathname.split('/')[6] ?? '';
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          schema_version: 2,
          projection_version: 'control-center-mobile-e2e',
          scope: {
            key: `conversation:${conversationId}`,
            kind: 'conversation',
            conversation_id: conversationId
          },
          conversation: {
            conversation_id: conversationId,
            agent_id: 'e2e-test-agent',
            title: 'Mobile Control Center timeline',
            active_session_id: 'sess-mobile-control-center',
            status: 'active'
          },
          timeline: { items: mobileTimeline, has_more_before: true, before_cursor: 'mobile-before-12' },
          state: {
            state_version: 1,
            snapshot_generated_at: '2026-08-24T12:00:00Z',
            capabilities: [],
            active_turn: {},
            pending: {},
            active_session: {
              session_id: 'sess-mobile-control-center',
              status: 'active',
              completion_reason: null,
              todos: []
            }
          },
          queue: { messages: [], queued_count: 0 },
          runtime: {
            runtime_epoch: 'control-center-mobile-e2e',
            runtime_revision: 1,
            generated_at: '2026-08-24T12:00:00Z',
            has_active_turn: false,
            volatile_items: []
          },
          cursor: `cursor:${conversationId}`,
          server_time: '2026-08-24T12:00:00Z'
        })
      });
    });
    await page.route('**/api/v1/chat/v2/conversations/*/timeline**', async (route) => {
      timelineBackfillRequests += 1;
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          schema_version: 2,
          projection_version: 'control-center-mobile-e2e',
          conversation_id: 'mobile-control-center',
          scope: {
            key: 'conversation:mobile-control-center',
            kind: 'conversation',
            conversation_id: 'mobile-control-center'
          },
          items: [],
          has_more_before: false,
          before_cursor: null,
          server_time: '2026-08-24T12:00:00Z'
        })
      });
    });
    await page.route('**/api/v1/conversations/*/read', async (route) => {
      markReadRequests += 1;
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ ok: true })
      });
    });
    await page.setViewportSize({ width: 390, height: 844 });
    await page.goto('/');

    await expect(page.getByTestId('control-center')).toBeVisible();
    await expect(page.getByTestId('dashboard-issues-healthy')).toHaveCount(0);
    await expect(page.getByTestId('dashboard-conversations-agents')).toBeVisible();
    await expect(page.getByTestId('dashboard-tasks-section')).toBeVisible();
    await expect(page.getByTestId('dashboard-conversations-section')).toBeVisible();

    const orderedSections = [
      page.getByTestId('dashboard-tasks-section'),
      page.getByTestId('dashboard-conversations-section')
    ];
    const verticalPositions = await Promise.all(orderedSections.map(async (section) => (await section.boundingBox())?.y ?? 0));
    expect(verticalPositions).toEqual([...verticalPositions].sort((left, right) => left - right));
    await expect(page.getByText('Checking system health…')).toBeHidden();
    await expect(page.getByText('Loading tasks…')).toBeHidden();
    await expect(page.getByText('Loading conversations…')).toBeHidden();

    const screenshotPath = testInfo.outputPath('control-center-mobile.png');
    await page.screenshot({ path: screenshotPath, fullPage: true, animations: 'disabled' });
    await testInfo.attach('control-center-mobile', { path: screenshotPath, contentType: 'image/png' });

    const conversationRows = page.locator('[data-testid^="dashboard-conversation-row-"]:not([data-testid*="-link-"])');
    await expect(conversationRows.first()).toBeVisible();
    await conversationRows.first().click();
    await expect(page.getByTestId('dashboard-conversation-chat')).toBeVisible();
    const mobileChat = page.getByTestId('dashboard-conversation-chat').getByTestId('compact-conversation-chat');
    const mobileViewport = mobileChat.getByTestId('scoped-timeline-viewport');
    await expect(mobileChat.getByText('Loading timeline…')).toBeHidden({ timeout: 30_000 });
    await expect(mobileViewport.locator('[data-timeline-row-key]')).toHaveCount(12);
    await expect(mobileChat.getByText(/Internal server error/i)).toHaveCount(0);
    await expect(mobileChat.getByRole('alert')).toHaveCount(0);
    await expect(mobileChat).toHaveAttribute('data-auto-tail', 'following');
    await expect.poll(() => mobileViewport.evaluate((node) => {
      const element = node as HTMLElement;
      return {
        overflow: element.scrollHeight > element.clientHeight,
        atTail: element.scrollTop + element.clientHeight >= element.scrollHeight - 2
      };
    })).toEqual({ overflow: true, atTail: true });
    await page.waitForTimeout(250);
    expect(timelineBackfillRequests).toBe(0);
    expect(markReadRequests).toBe(1);
    const workspace = page.getByTestId('conversation-modal-workspace');
    await workspace.evaluate((node) => { node.setAttribute('data-e2e-instance', 'stable'); });
    const modalComposer = mobileChat.getByTestId('compact-chat-composer');
    await modalComposer.fill('draft survives companion events');
    await mobileViewport.hover();
    await page.mouse.wheel(0, -5000);
    await expect(mobileChat).toHaveAttribute('data-auto-tail', 'paused');
    await expect.poll(() => timelineBackfillRequests).toBe(1);
    await page.evaluate(() => {
      const sockets = (window as typeof window & {
        __controlCenterSockets?: WebSocket[];
      }).__controlCenterSockets ?? [];
      const events = [
        { type: 'turn_started', conversation_id: 'conversation-e2e' },
        { type: 'message_complete', conversation_id: 'conversation-e2e' },
        {
          type: 'conversation_updated',
          conversation_id: 'conversation-e2e',
          has_active_turn: true
        },
        { type: 'conversation_runtime_snapshot', conversation_id: 'conversation-e2e' },
        { type: 'turn_settled', conversation_id: 'conversation-e2e' }
      ];
      for (let repeat = 0; repeat < 3; repeat += 1) {
        for (const event of events) {
          sockets.forEach((socket) => socket.dispatchEvent(new MessageEvent('message', {
            data: JSON.stringify(event)
          })));
        }
      }
    });
    await page.waitForTimeout(250);
    await expect(workspace).toHaveAttribute('data-e2e-instance', 'stable');
    await expect(modalComposer).toHaveValue('draft survives companion events');
    await expect(mobileChat).toHaveAttribute('data-auto-tail', 'paused');
    await expect(mobileChat.getByText('Loading timeline…')).toHaveCount(0);
    const mobileInspectorToggle = page.getByTestId('dashboard-entity-modal-inspector');
    await expect(page.locator(
      '[data-testid^="dashboard-conversation-inspector-"][data-testid$="-mobile-toggle"]'
    )).toHaveCount(0);
    await mobileInspectorToggle.click();
    const mobileInspector = page.locator(
      '[data-testid^="dashboard-conversation-inspector-"][data-testid$="-mobile"]'
    );
    await expect(mobileInspector).toBeVisible();
    await expect(mobileInspector.getByRole('tab', { name: 'Overview' })).toHaveAttribute('aria-selected', 'true');
    await mobileInspectorToggle.click();
    await expect(page.getByTestId('dashboard-conversation-chat')).toBeVisible();
    await expect(mobileChat.getByText('Loading timeline…')).toBeHidden({ timeout: 30_000 });
    // Opening the inspector must not discard the user's explicit scroll-up.
    await expect(mobileChat).toHaveAttribute('data-auto-tail', 'paused');
    expect(timelineBackfillRequests).toBe(1);
    expect(markReadRequests).toBe(1);
    await expect(page.getByTestId('dashboard-entity-modal').locator('.animate-pulse')).toHaveCount(0, {
      timeout: 30_000
    });

    const modalScreenshotPath = testInfo.outputPath('control-center-modal-mobile.png');
    await page.screenshot({ path: modalScreenshotPath, fullPage: true, animations: 'disabled' });
    await testInfo.attach('control-center-modal-mobile', {
      path: modalScreenshotPath,
      contentType: 'image/png'
    });
  });

  test('renders the approved rich running-task row on a wide dashboard', async ({ page }, testInfo) => {
    await page.setViewportSize({ width: 1600, height: 1000 });
    await page.route('**/api/v1/tasks/board**', async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          columns: {
            running: {
              total_count: 1,
              has_more: false,
              items: [{
                task_id: 'task-control-center-visual',
                title: 'Finalize Control Center experience',
                agent_id: 'laforge',
                status: 'running',
                priority: 1,
                created_at: '2026-08-23T06:30:00Z',
                updated_at: '2026-08-23T07:25:00Z',
                started_at: '2026-08-23T06:45:00Z',
                completed_at: null,
                progress_summary: {
                  todo_total: 7,
                  todo_completed: 4,
                  todo_in_progress: 1,
                  current_step_name: 'Polish responsive dashboard',
                  current_step_status: 'running',
                  changed_files: 12,
                  additions: 386,
                  deletions: 94
                }
              }]
            },
            done: { total_count: 0, has_more: false, items: [] }
          }
        })
      });
    });
    await login(page);
    await expect(page).toHaveURL(/\/$/);
    await expect(page.getByRole('link', { name: 'Open Control Center' })).toBeVisible();

    const row = page.getByTestId('dashboard-task-row-task-control-center-visual');
    await expect(row).toBeVisible();
    await expect(row.getByTestId('workstream-todo-progress')).toBeVisible();
    await expect(row.getByTestId('activity-avatar-orbit')).toBeVisible();
    await expect(page.getByTestId('dashboard-tasks-section').getByTestId('activity-avatar-orbit')).toHaveCount(1);
    await expect(row.getByTestId('diff-stat')).toBeVisible();
    await expect(row).toContainText('Polish responsive dashboard');
    await expect(row).toContainText('4/7 todos');
    await expect(page.getByText('Checking system health…')).toBeHidden();
    await expect(page.getByText('Loading conversations…')).toBeHidden();

    const screenshotPath = testInfo.outputPath('control-center-rich-desktop.png');
    await page.screenshot({ path: screenshotPath, fullPage: true, animations: 'disabled' });
    await testInfo.attach('control-center-rich-desktop', {
      path: screenshotPath,
      contentType: 'image/png'
    });
  });

  test('opens non-blocking chat and task workspace windows on desktop', async ({ page }, testInfo) => {
    await page.setViewportSize({ width: 1440, height: 1000 });
    await login(page);
    await installTaskCockpitFixture(page, { dashboardWorkspaceWindows: true });
    let workspaceTimelineBackfills = 0;
    page.on('request', (request) => {
      if (
        request.method() === 'GET'
        && /\/api\/v1\/chat\/v2\/(?:conversations\/[^/]+|scopes\/[^/]+)\/timeline/.test(
          new URL(request.url()).pathname,
        )
      ) workspaceTimelineBackfills += 1;
    });
    await page.reload();
    if (await page.getByTestId('workspace-layer').count() === 0) await page.reload();
    await expect(page.getByTestId('workspace-layer')).toBeVisible();

    await page.locator('[data-testid^="dashboard-agent-avatar-"]').first().click();
    const agentWindow = page.locator('[data-testid^="workspace-window-agent:"]').first();
    await expect(agentWindow).toBeVisible();
    await expect(agentWindow).toHaveAttribute('aria-modal', 'false');
    await expect(page.locator('[data-blocking-overlay]')).toHaveCount(0);
    const agentWorkspace = agentWindow.getByTestId('conversation-modal-workspace');
    await expect(agentWorkspace).toBeVisible();
    await agentWorkspace.evaluate((node) => node.setAttribute('data-e2e-instance', 'agent-stable'));
    const agentComposer = agentWindow.getByTestId('compact-chat-composer');
    await agentComposer.fill('Independent agent draft');

    await agentWindow.locator('[data-testid^="workspace-window-minimize-"]').evaluate((element) => {
      (element as HTMLButtonElement).click();
    });
    await expect(agentWindow).toHaveAttribute('data-minimized', 'true');
    await expect(page.getByTestId('workspace-dock')).toBeVisible();
    await page.getByTestId(`dashboard-task-row-${TASK_ID}`).click({ force: true });
    const taskWindow = page.getByTestId(`workspace-window-task:${TASK_ID}`);
    await expect(taskWindow).toBeVisible();
    await expect(page.locator('[data-testid^="workspace-window-"][role="dialog"]')).toHaveCount(2);
    await taskWindow.getByTestId('dashboard-task-modal-tab-control').click();
    const taskControl = taskWindow.getByTestId('dashboard-task-control');
    await expect(taskControl).toBeVisible();
    await expect(taskControl).toContainText('paused');
    await expect(taskControl).toContainText('approve');
    await expect(taskControl.getByRole('button', { name: 'Resolve the pending request to resume' })).toBeDisabled();
    await taskWindow.getByTestId('dashboard-task-modal-tab-control-chat').click();
    await expect(taskWindow.getByTestId('task-control-native-chat')).toBeVisible();
    const taskComposer = taskWindow.getByTestId('task-control-composer');
    await taskComposer.fill('Independent task draft');

    await taskWindow.getByLabel('Window layout').click();
    await taskWindow.getByRole('menuitem', { name: 'Right 50%' }).click();
    await expect.poll(() => taskWindow.evaluate((node) => {
      const rect = (node as HTMLElement).getBoundingClientRect();
      return rect.left >= window.innerWidth / 2 - 2 && rect.width <= window.innerWidth / 2 + 2;
    })).toBe(true);
    const dockGap = async () => {
      const [windowBox, dockBox] = await Promise.all([
        taskWindow.boundingBox(),
        page.getByTestId('workspace-dock').boundingBox(),
      ]);
      if (!windowBox || !dockBox) return Number.NEGATIVE_INFINITY;
      return dockBox.y - (windowBox.y + windowBox.height);
    };
    await expect.poll(dockGap).toBeGreaterThanOrEqual(11.5);
    const initialDockTop = (await page.getByTestId('workspace-dock').boundingBox())?.y ?? 0;
    await page.evaluate(() => {
      document.documentElement.style.setProperty('--app-shell-bottom-offset', '48px');
    });
    await expect.poll(async () => (
      (await page.getByTestId('workspace-dock').boundingBox())?.y ?? initialDockTop
    )).toBeLessThan(initialDockTop - 40);
    await expect.poll(dockGap).toBeGreaterThanOrEqual(11.5);
    await page.evaluate(() => {
      document.documentElement.style.setProperty('--app-shell-bottom-offset', '0px');
    });
    await expect.poll(async () => (
      (await page.getByTestId('workspace-dock').boundingBox())?.y ?? 0
    )).toBeGreaterThan(initialDockTop - 1);
    await expect.poll(dockGap).toBeGreaterThanOrEqual(11.5);
    const agentDockItem = page.locator('[data-testid^="workspace-dock-restore-agent:"]');
    const taskDockItem = page.getByTestId(`workspace-dock-restore-task:${TASK_ID}`);
    const dockOrder = async () => page.getByTestId('workspace-dock')
      .locator('[data-workspace-switcher-key]')
      .evaluateAll((items) => items.map((item) => item.getAttribute('data-workspace-switcher-key')));
    const dockTransformsSettled = async () => page.getByTestId('workspace-dock')
      .locator('[data-workspace-switcher-key]')
      .evaluateAll((items) => items.every((item) => {
        const transform = getComputedStyle(item).transform;
        if (transform === 'none') return true;
        const matrix = new DOMMatrixReadOnly(transform);
        return Math.abs(matrix.m41) < 0.5 && Math.abs(matrix.m42) < 0.5;
      }));
    const initialDockOrder = await dockOrder();
    const [agentDockBox, agentDockItemBox, taskDockBox, taskDockItemBox] = await Promise.all([
      agentDockItem.boundingBox(),
      agentDockItem.locator('..').boundingBox(),
      taskDockItem.boundingBox(),
      taskDockItem.locator('..').boundingBox(),
    ]);
    if (!agentDockBox || !agentDockItemBox || !taskDockBox || !taskDockItemBox) {
      throw new Error('Workspace switcher items are missing');
    }
    const pointerStartX = taskDockBox.x + taskDockBox.width / 2;
    const pointerStartY = taskDockBox.y + taskDockBox.height / 2;
    const grabOffsetX = pointerStartX - taskDockItemBox.x;
    const grabOffsetY = pointerStartY - taskDockItemBox.y;
    await page.mouse.move(
      pointerStartX,
      pointerStartY,
    );
    await page.mouse.down();
    const dragY = agentDockBox.y + agentDockBox.height / 2;
    const targetMidpointX = agentDockItemBox.x + agentDockItemBox.width / 2 - 10;
    await page.mouse.move((pointerStartX + targetMidpointX) / 2, dragY, {
      steps: 4,
    });
    await page.mouse.move(targetMidpointX, dragY, {
      steps: 4,
    });
    await expect(page.getByTestId('workspace-dock')).toHaveAttribute('data-dragging', 'true');
    await expect(page.getByTestId('workspace-dock-drop-indicator')).toBeVisible();
    await expect(taskDockItem.locator('..')).toHaveClass(/workspace-dock-item--placeholder/);
    await expect(page.getByTestId('workspace-dock-drag-ghost')).toHaveCount(1);
    const ghostBox = await page.getByTestId('workspace-dock-drag-ghost').boundingBox();
    if (!ghostBox) throw new Error('Workspace drag ghost is missing');
    await expect(page.getByTestId('workspace-dock-drag-ghost')).toHaveCSS('z-index', '80');
    const expectedGhostCenterX = targetMidpointX - grabOffsetX + taskDockItemBox.width / 2;
    const expectedGhostCenterY = dragY - grabOffsetY + taskDockItemBox.height / 2 - 3;
    expect(Math.abs(ghostBox.x + ghostBox.width / 2 - expectedGhostCenterX)).toBeLessThanOrEqual(2);
    expect(Math.abs(ghostBox.y + ghostBox.height / 2 - expectedGhostCenterY)).toBeLessThanOrEqual(2);
    expect(Math.abs(ghostBox.width / taskDockItemBox.width - 1.03)).toBeLessThanOrEqual(0.01);
    expect(Math.abs(ghostBox.height / taskDockItemBox.height - 1.03)).toBeLessThanOrEqual(0.01);
    await expect(page.getByTestId('workspace-dock')).toHaveAttribute(
      'data-provisional-order',
      new RegExp(`task:${TASK_ID},agent:`),
    );
    await expect.poll(() => agentDockItem.evaluate((element) => (
      element.parentElement?.getAttribute('style') ?? ''
    ))).toMatch(/translate3d/);
    const stablePreview = await page.getByTestId('workspace-dock').getAttribute('data-provisional-order');
    await page.evaluate(() => new Promise<void>((resolve) => {
      requestAnimationFrame(() => requestAnimationFrame(() => requestAnimationFrame(() => resolve())));
    }));
    await expect(page.getByTestId('workspace-dock')).toHaveAttribute(
      'data-provisional-order',
      stablePreview ?? '',
    );
    await expect(page.getByTestId('workspace-dock-drag-ghost')).toHaveCount(1);
    const dragFeedbackScreenshot = testInfo.outputPath('workspace-dock-drag-feedback.png');
    await page.screenshot({ path: dragFeedbackScreenshot, animations: 'disabled' });
    await testInfo.attach('workspace-dock-drag-feedback', {
      path: dragFeedbackScreenshot,
      contentType: 'image/png',
    });
    await page.mouse.up();
    const reorderedDockOrder = [
      `task:${TASK_ID}`,
      initialDockOrder.find((key) => key?.startsWith('agent:')),
    ];
    await expect.poll(dockOrder).toEqual(reorderedDockOrder);
    await expect.poll(() => page.evaluate((email) => {
      const key = `cognis.dashboard.workspace.v1:${encodeURIComponent(email.trim().toLowerCase())}`;
      const persisted = JSON.parse(localStorage.getItem(key) ?? '{}') as {
        windows?: Array<{ key?: string; switcherOrder?: number }>;
      };
      return persisted.windows
        ?.sort((left, right) => (left.switcherOrder ?? 0) - (right.switcherOrder ?? 0))
        .map((item) => item.key);
    }, ADMIN_EMAIL)).toEqual(reorderedDockOrder);
    await expect.poll(dockTransformsSettled).toBe(true);

    const [taskAfterLeft, agentAfterRight] = await Promise.all([
      taskDockItem.boundingBox(),
      agentDockItem.locator('..').boundingBox(),
    ]);
    if (!taskAfterLeft || !agentAfterRight) throw new Error('Reordered workspace switcher items are missing');
    await page.mouse.move(
      taskAfterLeft.x + taskAfterLeft.width / 2,
      taskAfterLeft.y + taskAfterLeft.height / 2,
    );
    await page.mouse.down();
    await page.mouse.move(
      agentAfterRight.x + agentAfterRight.width / 2,
      agentAfterRight.y + agentAfterRight.height / 2,
      { steps: 4 },
    );
    await page.mouse.move(
      agentAfterRight.x + agentAfterRight.width - 4,
      agentAfterRight.y + agentAfterRight.height / 2,
      { steps: 4 },
    );
    await expect(page.getByTestId('workspace-dock-drag-ghost')).toHaveCount(1);
    await expect(page.getByTestId('workspace-dock')).toHaveAttribute(
      'data-provisional-order',
      new RegExp(`agent:.*,task:${TASK_ID}`),
    );
    await page.mouse.up();
    await expect.poll(dockOrder).toEqual(initialDockOrder);
    await expect.poll(dockTransformsSettled).toBe(true);

    const [taskAfterRight, agentAfterLeft] = await Promise.all([
      taskDockItem.boundingBox(),
      agentDockItem.locator('..').boundingBox(),
    ]);
    if (!taskAfterRight || !agentAfterLeft) throw new Error('Restored workspace switcher items are missing');
    await page.mouse.move(
      taskAfterRight.x + taskAfterRight.width / 2,
      taskAfterRight.y + taskAfterRight.height / 2,
    );
    await page.mouse.down();
    await page.mouse.move(
      agentAfterLeft.x + agentAfterLeft.width / 2,
      agentAfterLeft.y + agentAfterLeft.height / 2,
      { steps: 4 },
    );
    await page.mouse.move(
      agentAfterLeft.x + 4,
      agentAfterLeft.y + agentAfterLeft.height / 2,
      { steps: 4 },
    );
    await page.mouse.up();
    await expect.poll(dockOrder).toEqual(reorderedDockOrder);

    await agentDockItem.click();
    await expect(agentDockItem).toHaveAttribute('aria-current', 'true');
    await expect(agentWindow).toHaveCSS('opacity', '1');
    await expect(agentWorkspace).toHaveAttribute('data-e2e-instance', 'agent-stable');
    await expect(agentComposer).toHaveValue('Independent agent draft');
    const agentChat = agentWindow.getByTestId('compact-conversation-chat');
    const agentTimeline = agentWindow.getByTestId('scoped-timeline-viewport');
    await expect(agentChat).toHaveAttribute('data-auto-tail', 'following');
    await expect.poll(() => agentTimeline.evaluate((node) => {
      const element = node as HTMLElement;
      return element.scrollHeight - element.scrollTop - element.clientHeight;
    })).toBeLessThanOrEqual(2);
    await agentTimeline.evaluate((node) => {
      node.setAttribute('data-e2e-node', 'timeline-stable');
      const state = window as typeof window & {
        __workspaceScrollSamples?: Array<{ scrollTop: number; autoTail: string | null }>;
        __stopWorkspaceScrollSamples?: () => void;
      };
      const samples: Array<{ scrollTop: number; autoTail: string | null }> = [];
      let active = true;
      const sample = (): void => {
        const element = node as HTMLElement;
        const chat = element.closest('[data-testid="compact-conversation-chat"]');
        const workspaceWindow = element.closest<HTMLElement>('[data-window-key]');
        if (
          workspaceWindow?.dataset.minimized !== 'true'
          && getComputedStyle(element).visibility !== 'hidden'
          && element.scrollHeight > element.clientHeight + 1
          && element.scrollTop <= 1
        ) {
          samples.push({
            scrollTop: element.scrollTop,
            autoTail: chat?.getAttribute('data-auto-tail') ?? null,
          });
        }
        if (active) requestAnimationFrame(sample);
      };
      state.__workspaceScrollSamples = samples;
      state.__stopWorkspaceScrollSamples = () => { active = false; };
      requestAnimationFrame(sample);
    });
    await expect(agentWindow.getByTestId('conversation-mobile-inspector-control')).toHaveCount(0);
    await taskDockItem.click();
    await expect(taskDockItem).toHaveAttribute('aria-current', 'true');
    await expect.poll(dockOrder).toEqual(reorderedDockOrder);
    await agentDockItem.click();
    await expect(agentDockItem).toHaveAttribute('aria-current', 'true');
    await taskDockItem.click();
    await agentDockItem.click();
    await expect(agentTimeline).toHaveAttribute('data-e2e-node', 'timeline-stable');
    await expect(agentChat).toHaveAttribute('data-auto-tail', 'following');
    await expect.poll(() => agentTimeline.evaluate((node) => {
      const element = node as HTMLElement;
      return element.scrollHeight - element.scrollTop - element.clientHeight;
    })).toBeLessThanOrEqual(2);
    expect(await page.evaluate(() => {
      const state = window as typeof window & {
        __workspaceScrollSamples?: Array<{ scrollTop: number; autoTail: string | null }>;
      };
      return state.__workspaceScrollSamples ?? [];
    })).toEqual([]);
    await taskDockItem.click();
    const compactHeaderGeometry = await agentWindow.evaluate((element) => {
      const header = element.querySelector<HTMLElement>('[data-testid^="workspace-window-drag-"]');
      const body = element.querySelector<HTMLElement>('[data-testid^="workspace-window-body-"]');
      const timeline = element.querySelector<HTMLElement>('[data-testid="scoped-timeline-viewport"]');
      if (!header || !body || !timeline) throw new Error('Conversation window geometry is incomplete');
      return {
        headerBottom: header.getBoundingClientRect().bottom,
        bodyTop: body.getBoundingClientRect().top,
        timelineTop: timeline.getBoundingClientRect().top,
      };
    });
    expect(Math.abs(compactHeaderGeometry.bodyTop - compactHeaderGeometry.headerBottom)).toBeLessThanOrEqual(1);
    expect(Math.abs(compactHeaderGeometry.timelineTop - compactHeaderGeometry.bodyTop)).toBeLessThanOrEqual(1);
    await agentTimeline.evaluate((node) => {
      const element = node as HTMLElement;
      element.scrollTop = Math.max(0, element.scrollHeight - element.clientHeight - 40);
      element.setAttribute('data-e2e-node', 'timeline-stable');
    });
    await expect(agentChat).toHaveAttribute('data-auto-tail', 'paused');

    await agentDockItem.click();
    await expect(agentDockItem).toHaveAttribute('aria-current', 'true');
    await expect.poll(dockOrder).toEqual(reorderedDockOrder);
    await agentTimeline.evaluate((node) => {
      const element = node as HTMLElement;
      element.scrollTop = Math.max(0, element.scrollHeight - element.clientHeight - 40);
    });
    const focusedAgentScrollTop = await agentTimeline.evaluate((node) => (node as HTMLElement).scrollTop);
    await page.evaluate(() => new Promise<void>((resolve) => {
      requestAnimationFrame(() => requestAnimationFrame(() => resolve()));
    }));
    await expect(agentTimeline).toHaveAttribute('data-e2e-node', 'timeline-stable');
    expect(await agentTimeline.evaluate((node) => (node as HTMLElement).scrollTop)).toBe(focusedAgentScrollTop);
    await expect(agentChat).toHaveAttribute('data-auto-tail', 'paused');
    await taskDockItem.click();
    await expect(taskDockItem).toHaveAttribute('aria-current', 'true');
    await expect.poll(dockOrder).toEqual(reorderedDockOrder);

    await agentDockItem.click();
    await expect(agentDockItem).toHaveAttribute('aria-current', 'true');
    await agentWindow.getByLabel('Minimize').click();
    await expect(agentWindow).toHaveAttribute('data-minimized', 'true');
    await agentDockItem.click();
    await expect(agentWindow).toHaveAttribute('data-minimized', 'false');
    await expect(agentTimeline).toHaveAttribute('data-e2e-node', 'timeline-stable');
    await expect(agentChat).toHaveAttribute('data-auto-tail', 'paused');
    expect(await agentTimeline.evaluate((node) => (node as HTMLElement).scrollTop)).toBe(focusedAgentScrollTop);
    await taskDockItem.click();
    await expect(taskDockItem).toHaveAttribute('aria-current', 'true');
    await expect(taskWindow).toHaveAttribute('data-minimized', 'false');
    const dashboardControl = page.getByTestId('workspace-dock-dashboard');
    await expect(dashboardControl).toHaveAttribute('title', 'Show dashboard');
    await dashboardControl.click();
    await expect(taskWindow).toHaveAttribute('data-minimized', 'true');
    await expect(agentWindow).toHaveAttribute('data-minimized', 'true');
    await expect(dashboardControl).toHaveAttribute('title', 'Restore windows');
    await expect(dashboardControl).toHaveAttribute('aria-pressed', 'true');
    await expect.poll(() => page.evaluate((email) => {
      const key = `cognis.dashboard.workspace.v1:${encodeURIComponent(email.trim().toLowerCase())}`;
      const persisted = JSON.parse(localStorage.getItem(key) ?? '{}') as {
        windows?: Array<{ key?: string; minimized?: boolean; switcherOrder?: number }>;
      };
      return persisted.windows
        ?.sort((left, right) => (left.switcherOrder ?? 0) - (right.switcherOrder ?? 0))
        .map((item) => [item.key, item.minimized]);
    }, ADMIN_EMAIL)).toEqual([
      [`task:${TASK_ID}`, false],
      [initialDockOrder.find((key) => key?.startsWith('agent:')), false],
    ]);
    await dashboardControl.click();
    await expect(taskWindow).toHaveAttribute('data-minimized', 'false');
    await expect(agentWindow).toHaveAttribute('data-minimized', 'false');
    await expect(taskDockItem).toHaveAttribute('aria-current', 'true');
    await expect.poll(dockOrder).toEqual(reorderedDockOrder);
    await agentDockItem.click();
    await expect(agentWindow).toHaveAttribute('data-minimized', 'false');
    await expect(agentTimeline).toHaveAttribute('data-e2e-node', 'timeline-stable');
    await expect(agentChat).toHaveAttribute('data-auto-tail', 'paused');
    expect(await agentTimeline.evaluate((node) => (node as HTMLElement).scrollTop)).toBe(focusedAgentScrollTop);
    expect(await page.evaluate(() => {
      const state = window as typeof window & {
        __workspaceScrollSamples?: Array<{ scrollTop: number; autoTail: string | null }>;
        __stopWorkspaceScrollSamples?: () => void;
      };
      state.__stopWorkspaceScrollSamples?.();
      return state.__workspaceScrollSamples ?? [];
    })).toEqual([]);
    expect(workspaceTimelineBackfills).toBe(0);

    const grip = taskWindow.getByLabel('Resize window');
    await expect(grip).toHaveCSS('width', '12px');
    await expect(grip).toHaveCSS('height', '12px');
    await expect(page.getByTestId('workspace-veil')).toHaveCSS('pointer-events', 'none');

    await expect(taskComposer).toHaveValue('Independent task draft');
    await taskWindow.getByLabel('Minimize').click();
    await agentDockItem.click();
    await expect(agentDockItem).toHaveAttribute('aria-current', 'true');
    await agentWindow.getByLabel('Minimize').click();
    await taskDockItem.click();
    await expect(page.locator('[data-testid^="workspace-window-"][role="dialog"]')).toHaveCount(2);
    await expect(taskWindow).toHaveAttribute('data-minimized', 'false');
    await expect(taskComposer).toHaveValue('Independent task draft');
    await taskWindow.getByLabel('Minimize').click();
    await agentDockItem.click();
    await expect(agentWindow).toHaveAttribute('data-minimized', 'false');
    await page.getByTestId('dashboard-tasks-new').evaluate((element) => {
      (element as HTMLButtonElement).click();
    });
    const blockingCreate = page.getByRole('dialog', { name: 'Create task' });
    await expect(blockingCreate).toBeVisible();
    await expect(blockingCreate).toHaveAttribute('aria-modal', 'true');
    await expect(agentWindow).toBeVisible();
    await blockingCreate.getByLabel('Close').click();
    await expect(agentComposer).toHaveValue('Independent agent draft');
    await page.getByTestId(`workspace-dock-close-task:${TASK_ID}`).click();
    await expect(page.getByTestId(`workspace-dock-item-task:${TASK_ID}`)).toHaveCount(0);
    await expect(agentDockItem.locator('..')).toBeVisible();
    await expect(agentDockItem).toHaveAttribute('aria-current', 'true');
    await expect(page.getByTestId('workspace-dock-dashboard')).toHaveAttribute('title', 'Show dashboard');

    const screenshotPath = testInfo.outputPath('control-center-workspace-desktop.png');
    await page.screenshot({ path: screenshotPath, animations: 'disabled' });
    await testInfo.attach('control-center-workspace-desktop', { path: screenshotPath, contentType: 'image/png' });
  });

  test('exposes state-aware task controls without cancelling shared tasks', async ({ page }) => {
    await login(page);
    const fixture = await installTaskCockpitFixture(page, { dashboardWorkspaceWindows: true });
    fixture.setStatus('running');
    await page.reload();

    await page.waitForTimeout(250);
    await page.getByTestId(`dashboard-task-row-${TASK_ID}`).click();
    const taskSurface = page.getByTestId(`workspace-window-task:${TASK_ID}`);
    await taskSurface.getByTestId('dashboard-task-modal-tab-control').click();
    const control = taskSurface.getByTestId('dashboard-task-control');
    await expect(control).toContainText('running');
    await expect(control).toContainText('fetch');
    await expect(control.getByRole('button', { name: 'Pause task' })).toBeVisible();
    await expect(control.getByRole('button', { name: 'Cancel task' })).toBeVisible();

    await control.getByRole('button', { name: 'Pause task' }).click();
    await expect.poll(() => fixture.actionRequests()).toContain(`POST /api/v1/tasks/${TASK_ID}/pause`);
    await expect(control.getByRole('status')).toContainText('Task paused.');
  });

  test('moves a cancelled Control task to Recent without losing scope or loaded tail', async ({ page }) => {
    await login(page);
    const fixture = await installTaskCockpitFixture(page, { dashboardWorkspaceWindows: true });
    fixture.setStatus('running');
    const boardRequests: URL[] = [];
    await page.route(/\/api\/v1\/projects(?:\?.*)?$/, async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify([{
          project_id: 'project-control',
          name: 'Control project',
          description: null,
          source_path: null,
          remote_url: null,
          settings: {},
          created_at: '2026-08-31T12:00:00Z',
          updated_at: '2026-08-31T12:00:00Z',
        }]),
      });
    });
    await page.route('**/api/v1/tasks/board**', async (route) => {
      const url = new URL(route.request().url());
      boardRequests.push(url);
      const scoped = (
        url.searchParams.get('q') === 'release'
        && url.searchParams.get('project_id') === 'project-control'
      );
      const cancelled = fixture.actionRequests().includes(`POST /api/v1/tasks/${TASK_ID}/cancel`);
      const boardItem = (taskId: string, title: string, status: string) => ({
        task_id: taskId,
        title,
        agent_id: 'riker',
        project_id: 'project-control',
        status,
        priority: 1,
        created_at: '2026-08-31T12:00:00Z',
        updated_at: '2026-08-31T12:00:00Z',
        started_at: '2026-08-31T12:00:00Z',
        completed_at: status === 'cancelled' || status === 'completed'
          ? '2026-08-31T12:00:00Z'
          : null,
        progress_summary: null,
      });
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          columns: {
            running: {
              items: cancelled ? [] : [boardItem(TASK_ID, 'Release safety review', 'running')],
              groups: [],
              cursor: null,
              has_more: false,
              total_count: cancelled ? 0 : 1,
            },
            done: {
              items: cancelled
                ? [
                    boardItem(TASK_ID, 'Release safety review', 'cancelled'),
                    boardItem('recent-head-task', 'Recent page head', 'completed'),
                  ]
                : [boardItem('recent-head-task', 'Recent page head', 'completed')],
              groups: [],
              cursor: scoped && !cancelled ? 'loaded-tail-cursor' : null,
              has_more: scoped && !cancelled,
              total_count: cancelled ? 2 : 1,
            },
          },
        }),
      });
    });
    let tailRequests = 0;
    await page.route('**/api/v1/tasks/board/done**', async (route) => {
      tailRequests += 1;
      const url = new URL(route.request().url());
      expect(url.searchParams.get('cursor')).toBe('loaded-tail-cursor');
      expect(url.searchParams.get('q')).toBe('release');
      expect(url.searchParams.get('project_id')).toBe('project-control');
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          items: [{
            task_id: 'loaded-tail-task',
            title: 'Loaded Recent tail',
            agent_id: 'riker',
            project_id: 'project-control',
            status: 'completed',
            priority: 1,
            created_at: '2026-08-31T12:00:00Z',
            updated_at: '2026-08-31T12:00:00Z',
            started_at: '2026-08-31T12:00:00Z',
            completed_at: '2026-08-31T12:00:00Z',
            progress_summary: null,
          }],
          groups: [],
          cursor: null,
          has_more: false,
          total_count: 1,
        }),
      });
    });

    await page.reload();
    await page.getByLabel('Search tasks and conversations').fill('release');
    await page.getByLabel('Filter dashboard by project').selectOption('project-control');
    await expect.poll(() => boardRequests.some((url) => (
      url.searchParams.get('q') === 'release'
      && url.searchParams.get('project_id') === 'project-control'
    ))).toBe(true);
    await expect(page.getByTestId(`dashboard-task-row-${TASK_ID}`)).toBeVisible();
    await expect(page.getByTestId('dashboard-task-row-loaded-tail-task')).toBeVisible();
    expect(tailRequests).toBe(1);
    await page.waitForTimeout(250);
    await page.getByTestId(`dashboard-task-row-${TASK_ID}`).click();
    const taskSurface = page.getByTestId(`workspace-window-task:${TASK_ID}`);
    await taskSurface.getByTestId('dashboard-task-modal-tab-control').click();
    await taskSurface.getByRole('button', { name: 'Cancel task' }).click();
    const confirmation = page.getByRole('dialog', { name: 'Cancel task?' });
    await confirmation.getByRole('button', { name: 'Cancel task', exact: true }).click();

    await expect(page.getByTestId('dashboard-tasks-running')).not.toContainText('Release safety review');
    await expect(page.getByTestId(`dashboard-task-row-${TASK_ID}`)).toBeVisible();
    await expect(page.getByTestId('dashboard-tasks-recent')).toContainText('Release safety review');
    await expect(page.getByTestId('dashboard-task-row-loaded-tail-task')).toBeVisible();
    await expect(page.getByTestId(`dashboard-task-row-${TASK_ID}`)).toHaveCount(1);
    await expect(page.getByLabel('Search tasks and conversations')).toHaveValue('release');
    await expect(page.getByLabel('Filter dashboard by project')).toHaveValue('project-control');
    await expect.poll(() => boardRequests.some((url) => (
      url.searchParams.get('q') === 'release'
      && url.searchParams.get('project_id') === 'project-control'
    ))).toBe(true);
  });

  test('restores minimized windows and adapts conversation content to window width', async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 1000 });
    await login(page);
    await installTaskCockpitFixture(page, { dashboardWorkspaceWindows: true });
    let markReadRequests = 0;
    await page.route('**/api/v1/conversations/*/read', async (route) => {
      markReadRequests += 1;
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ ok: true }),
      });
    });
    await page.reload();
    if (await page.getByTestId('workspace-layer').count() === 0) await page.reload();

    await page.locator('[data-testid^="dashboard-agent-avatar-"]').first().click();
    const agentWindow = page.locator('[data-testid^="workspace-window-agent:"]').first();
    const workspace = agentWindow.getByTestId('conversation-modal-workspace');
    await expect(workspace).toHaveAttribute('data-layout', 'wide');
    const desktopInspector = agentWindow.locator(
      '[data-testid^="dashboard-conversation-inspector-"][data-testid$="-desktop"]',
    );
    const desktopResizer = agentWindow.getByRole('separator', { name: 'Resize conversation inspector' });
    const initialInspectorWidth = Number.parseFloat(await desktopInspector.evaluate(
      (element) => getComputedStyle(element).width,
    ));
    expect(initialInspectorWidth).toBeGreaterThanOrEqual(320);
    expect(initialInspectorWidth).toBeLessThanOrEqual(400);
    await expect.poll(async () => {
      const before = await desktopResizer.boundingBox();
      await page.waitForTimeout(300);
      const after = await desktopResizer.boundingBox();
      if (!before || !after) return false;
      return Math.abs(before.x - after.x) < 1 && Math.abs(before.y - after.y) < 1;
    }).toBe(true);
    const resizerBox = await desktopResizer.boundingBox();
    if (!resizerBox) throw new Error('Conversation inspector resizer is missing');
    await page.mouse.move(resizerBox.x + resizerBox.width / 2, resizerBox.y + 30);
    await page.mouse.down();
    await page.mouse.move(resizerBox.x - 40, resizerBox.y + 30);
    await page.mouse.up();
    await expect.poll(async () => Number.parseFloat(await desktopInspector.evaluate(
      (element) => getComputedStyle(element).width,
    ))).toBeGreaterThanOrEqual(initialInspectorWidth + 39);

    const windowGrip = agentWindow.getByLabel('Resize window');
    const gripBox = await windowGrip.boundingBox();
    if (!gripBox) throw new Error('Workspace resize handle is missing');
    await page.mouse.move(gripBox.x + gripBox.width / 2, gripBox.y + gripBox.height / 2);
    await page.mouse.down();
    await page.mouse.move(gripBox.x + gripBox.width / 2, gripBox.y - 1000);
    await page.mouse.up();
    await expect(agentWindow).toHaveCSS('height', '320px');
    const compactLayout = await agentWindow.evaluate((element) => {
      const auxiliary = element.querySelector<HTMLElement>('[data-testid="compact-chat-auxiliary"]');
      const timeline = element.querySelector<HTMLElement>('[data-scope-key]');
      const composer = element.querySelector<HTMLElement>('.compact-chat-composer');
      const body = element.querySelector<HTMLElement>('[data-testid^="workspace-window-body-"]');
      if (!auxiliary || !timeline || !composer || !body) {
        throw new Error('Compact chat layout is incomplete');
      }
      for (const height of [160, 160]) {
        const probe = document.createElement('div');
        probe.style.height = `${height}px`;
        probe.style.flex = '0 0 auto';
        auxiliary.appendChild(probe);
      }
      const bodyBox = body.getBoundingClientRect();
      const timelineBox = timeline.getBoundingClientRect();
      const composerBox = composer.getBoundingClientRect();
      return {
        auxiliaryHeight: auxiliary.clientHeight,
        auxiliaryScrollHeight: auxiliary.scrollHeight,
        bodyBottom: bodyBox.bottom,
        composerBottom: composerBox.bottom,
        composerHeight: composerBox.height,
        timelineHeight: timelineBox.height,
      };
    });
    expect(compactLayout.auxiliaryScrollHeight).toBeGreaterThan(compactLayout.auxiliaryHeight);
    expect(compactLayout.composerHeight).toBeGreaterThan(0);
    expect(compactLayout.timelineHeight).toBeGreaterThan(0);
    expect(compactLayout.composerBottom).toBeLessThanOrEqual(compactLayout.bodyBottom + 1);

    await agentWindow.getByLabel('Window layout').click();
    await agentWindow.getByRole('menuitem', { name: 'Left 50%' }).click();
    await expect(workspace).toHaveAttribute('data-layout', 'narrow');
    await expect(desktopInspector).toHaveCount(0);

    await agentWindow.getByLabel('Minimize', { exact: true }).click();
    const readsBeforeMinimizedReload = markReadRequests;
    await expect.poll(() => page.evaluate((email) => {
      const key = `cognis.dashboard.workspace.v1:${encodeURIComponent(email.trim().toLowerCase())}`;
      const persisted = JSON.parse(localStorage.getItem(key) ?? '{}') as {
        windows?: Array<{ key?: string; minimized?: boolean }>;
      };
      return persisted.windows?.find((item) => item.key?.startsWith('agent:'))?.minimized ?? null;
    }, ADMIN_EMAIL)).toBe(true);
    await expect(agentWindow).toHaveAttribute('data-minimized', 'true');

    await page.reload();
    const restoredAgent = page.locator('[data-testid^="workspace-window-agent:"]').first();
    await expect(restoredAgent).toHaveAttribute('data-minimized', 'true');
    await page.waitForTimeout(250);
    expect(markReadRequests).toBe(readsBeforeMinimizedReload);
    await expect(page.getByTestId('workspace-dock')).toBeVisible();
    await page.locator('[data-testid^="workspace-dock-restore-agent:"]').click();
    await expect(restoredAgent).toHaveAttribute('data-minimized', 'false');
    await expect.poll(() => markReadRequests).toBe(readsBeforeMinimizedReload + 1);
    const restoredWorkspace = restoredAgent.getByTestId('conversation-modal-workspace');
    await expect(restoredWorkspace).toHaveAttribute('data-layout', 'narrow');

    const narrowToggle = restoredAgent.locator('[data-testid^="workspace-window-inspector-agent:"]');
    await expect(restoredAgent.getByTestId('conversation-mobile-inspector-control')).toHaveCount(0);
    await expect(narrowToggle).toHaveAttribute('aria-expanded', 'false');
    await narrowToggle.click();
    await expect(narrowToggle).toHaveAttribute('aria-expanded', 'true');
    await expect(restoredAgent.getByTestId('dashboard-conversation-chat')).toBeVisible();
    await expect(restoredAgent.locator(
      '[data-testid^="dashboard-conversation-inspector-"][data-testid$="-mobile"]',
    )).toBeVisible();
    await page.keyboard.press('Escape');
    await expect(restoredAgent.locator(
      '[data-testid^="dashboard-conversation-inspector-"][data-testid$="-mobile"]',
    )).toHaveCount(0);
    await expect(narrowToggle).toBeFocused();
    await expect(narrowToggle).toHaveAttribute('aria-expanded', 'false');
    await narrowToggle.click();
    await expect(narrowToggle).toHaveAttribute('aria-expanded', 'true');
    await page.getByTestId('dashboard-tasks-new').evaluate((element) => {
      (element as HTMLButtonElement).click();
    });
    const blockingCreate = page.getByRole('dialog', { name: 'Create task' });
    await expect(blockingCreate).toBeVisible();
    await page.keyboard.press('Escape');
    await expect(blockingCreate).toHaveCount(0);
    await expect(page.locator('[data-blocking-overlay]')).toHaveCount(0);
    await expect(restoredAgent.locator(
      '[data-testid^="dashboard-conversation-inspector-"][data-testid$="-mobile"]',
    )).toBeVisible();
    await expect(restoredAgent).toBeVisible();
    await expect(narrowToggle).toHaveAttribute('aria-expanded', 'true');
  });

  test('uses snap-first tablet windows and preserves phone blocking modals', async ({ page }, testInfo) => {
    await page.setViewportSize({ width: 820, height: 1180 });
    await login(page);
    await installTaskCockpitFixture(page, { dashboardWorkspaceWindows: true });
    await page.reload();
    if (await page.getByTestId('workspace-layer').count() === 0) await page.reload();
    await expect(page.getByTestId('workspace-layer')).toBeVisible();
    const tabletAvatar = page.locator('[data-testid^="dashboard-agent-avatar-"]').first();
    await expect.poll(async () => {
      const before = await tabletAvatar.boundingBox();
      await page.waitForTimeout(300);
      const after = await tabletAvatar.boundingBox();
      if (!before || !after) return false;
      return Math.abs(before.x - after.x) < 1 && Math.abs(before.y - after.y) < 1;
    }).toBe(true);
    await tabletAvatar.click({ force: true });
    const tabletAgent = page.locator('[data-testid^="workspace-window-agent:"]').first();
    await expect(tabletAgent).toBeVisible();
    await tabletAgent.getByLabel('Minimize').click();
    await expect(tabletAgent).toHaveAttribute('data-minimized', 'true');
    await expect(page.getByTestId('workspace-dock')).toBeVisible();
    await page.getByTestId(`dashboard-task-row-${TASK_ID}`).click();
    const tabletTask = page.getByTestId(`workspace-window-task:${TASK_ID}`);
    await expect(tabletTask).toBeVisible();
    await page.locator('[data-testid^="workspace-dock-restore-agent:"]').click();
    await expect.poll(async () => {
      const [agent, task] = await Promise.all([tabletAgent.boundingBox(), tabletTask.boundingBox()]);
      return Math.abs((agent?.width ?? 0) - (task?.width ?? 0));
    }).toBeLessThanOrEqual(2);
    const [agentBox, taskBox] = await Promise.all([tabletAgent.boundingBox(), tabletTask.boundingBox()]);
    expect(taskBox?.x).toBeGreaterThan(agentBox?.x ?? Infinity);
    const mobileHeader = page.locator('header').filter({
      has: page.getByRole('heading', { name: 'Control Center' }),
    });
    const headerBox = await mobileHeader.boundingBox();
    const navBox = await page.getByRole('navigation', { name: 'Primary' }).boundingBox();
    expect((agentBox?.y ?? 0)).toBeGreaterThanOrEqual(
      (headerBox?.y ?? 0) + (headerBox?.height ?? 0),
    );
    expect((agentBox?.y ?? 0) + (agentBox?.height ?? 0)).toBeLessThanOrEqual(navBox?.y ?? 1180);
    const tabletPath = testInfo.outputPath('control-center-workspace-tablet.png');
    await page.screenshot({ path: tabletPath, animations: 'disabled' });
    await testInfo.attach('control-center-workspace-tablet', { path: tabletPath, contentType: 'image/png' });

    await page.setViewportSize({ width: 390, height: 844 });
    await page.reload();
    await page.getByTestId(`dashboard-task-row-${TASK_ID}`).click();
    await expect(page.locator('[data-testid^="workspace-window-"][role="dialog"]')).toHaveCount(0);
    await expect(page.getByTestId('dashboard-entity-modal')).toBeVisible();
    await expect(page.locator('[data-blocking-overlay]')).toBeVisible();
    const phonePath = testInfo.outputPath('control-center-workspace-phone-fallback.png');
    await page.screenshot({ path: phonePath, animations: 'disabled' });
    await testInfo.attach('control-center-workspace-phone-fallback', { path: phonePath, contentType: 'image/png' });
  });

  test('PWA task, upcoming schedule, and agent modals keep safe controls and real task scopes', async ({ page }, testInfo) => {
    await login(page);
    const cockpitFixture = await installTaskCockpitFixture(page, {
      dashboardWorkspaceWindows: true
    });
    await page.route('**/api/v1/tasks/board**', async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          columns: {
            running: {
              total_count: 1,
              has_more: false,
              items: [{
                task_id: TASK_ID,
                title: 'Release safety review',
                agent_id: 'riker',
                status: 'running',
                priority: 1,
                created_at: '2026-08-23T06:30:00Z',
                updated_at: '2026-08-23T07:25:00Z',
                started_at: '2026-08-23T06:45:00Z',
                completed_at: null,
                progress_summary: null
              }]
            },
            done: { total_count: 0, has_more: false, items: [] }
          }
        })
      });
    });
    const scheduleFixture = {
      schedule_id: 'schedule-pwa',
      name: 'Daily release review',
      description: 'Review release readiness. '.repeat(80),
      schedule_type: 'cron',
      cron_expr: '0 8 * * *',
      interval_seconds: null,
      one_shot_at: null,
      timezone: 'UTC',
      agent_id: 'riker',
      workflow_id: 'workflow-stage39',
      project_id: 'project-release',
      skill_id: null,
      task_template: { title: 'Release review', description: 'Review the release. '.repeat(80) },
      enabled: true,
      max_concurrent_runs: 1,
      delete_after_run: false,
      retry_failed_tasks: false,
      fail_paused_task_on_next_fire: false,
      completion_mode_family: 'default',
      allow_silent_completion: false,
      interaction_mode_override: null,
      session_policy: null,
      last_fired_at: null,
      next_fire_at: '2026-08-24T08:00:00Z',
      last_run_status: null,
      consecutive_errors: 0,
      disabled_reason: null,
      created_by: 'admin@example.com',
      created_at: '2026-08-23T00:00:00Z',
      updated_at: '2026-08-23T00:00:00Z',
      human_schedule: 'Every day at 08:00 UTC',
      is_expired: false,
      expiration_grace_until: null
    };
    await page.route('**/api/v1/schedules**', async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify([scheduleFixture])
      });
    });
    await page.route('**/api/v1/schedules/schedule-pwa', async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(scheduleFixture)
      });
    });
    await page.route('**/api/v1/workflows/workflow-stage39', async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          workflow_id: 'workflow-stage39',
          name: 'Release workflow',
          description: 'Release review',
          version: 1,
          criteria: '',
          tags: [],
          interaction: {},
          defaults: {},
          steps: [
            { name: 'prepare', type: 'run', description: 'Prepare evidence' },
            { name: 'review', type: 'gate', description: 'Review evidence' }
          ],
          is_system: false,
          owner_email: 'admin@example.com',
          lifecycle: 'persistent',
          archived_at: null,
          lineage: null,
          editable_fields: [],
          has_overrides: false,
          disabled: false,
          disableable: true,
          override_warnings: []
        })
      });
    });
    await page.setViewportSize({ width: 390, height: 844 });
    await page.goto('/');
    const fixtureStepRuns = await page.evaluate(async (taskId) => {
      const response = await fetch(`/api/v1/tasks/${taskId}/summary`, { credentials: 'include' });
      const detail = await response.json();
      return detail.step_runs as unknown[];
    }, TASK_ID);
    expect(fixtureStepRuns.length).toBeGreaterThan(0);
    await page.evaluate(() => {
      document.documentElement.style.setProperty('--dashboard-safe-area-top', '24px');
      document.documentElement.style.setProperty('--dashboard-safe-area-bottom', '20px');
    });

    await page.getByTestId(`dashboard-task-row-${TASK_ID}`).click();
    await expect(page.getByTestId('dashboard-task-modal-tab-description')).toHaveAttribute('aria-selected', 'true');
    await expect(page.getByTestId('task-attention')).toBeVisible();
    await expect(page.getByTestId('dashboard-entity-modal-close')).toBeVisible();
    await expect(page.getByTestId('dashboard-entity-modal-external-link')).toBeVisible();
    const taskModalUrl = page.url();
    await page.getByTestId('dashboard-task-modal-tab-steps').click();
    await expect(
      page.getByTestId('dashboard-task-modal-panel-steps').locator('[data-testid="todo-status-dot"][data-todo-status="in_progress"]')
    ).toHaveAttribute('data-animated', 'true');
    const todoPath = testInfo.outputPath('control-center-todo-indicator.png');
    await page.screenshot({ path: todoPath, animations: 'disabled' });
    await testInfo.attach('control-center-todo-indicator', { path: todoPath, contentType: 'image/png' });
    await page.getByRole('button', { name: 'Output' }).first().click();
    const outputOverlay = page.getByTestId('step-output-overlay');
    const outputPanel = page.getByTestId('step-output-panel');
    await expect(outputPanel).toHaveAccessibleName('Step output: fetch');
    await expect(outputPanel.getByRole('heading', { name: 'fetch' })).toBeVisible();
    expect(await outputOverlay.evaluate((root) => root.parentElement === document.body)).toBe(true);
    expect(await outputOverlay.evaluate((root) => {
      const overlays = [...document.body.querySelectorAll<HTMLElement>('[data-overlay-id]')];
      return overlays[overlays.length - 1] === root;
    })).toBe(true);
    expect(await outputPanel.evaluate((panel) => {
      const rect = panel.getBoundingClientRect();
      const target = document.elementFromPoint(rect.left + rect.width / 2, rect.top + rect.height / 2);
      return Boolean(target?.closest('[data-testid="step-output-panel"]'));
    })).toBe(true);
    await expect(page.getByTestId('dashboard-entity-modal')).toBeVisible();
    expect(page.url()).toBe(taskModalUrl);
    await expect(outputPanel.getByRole('button', { name: 'Close' })).toBeVisible();
    const outputPath = testInfo.outputPath('control-center-task-step-output.png');
    await page.screenshot({ path: outputPath, animations: 'disabled' });
    await testInfo.attach('control-center-task-step-output', { path: outputPath, contentType: 'image/png' });
    await page.keyboard.press('Escape');
    await expect(outputOverlay).toHaveCount(0);
    await expect(page.getByTestId('dashboard-task-modal-panel-steps')).toBeVisible();
    await page.getByRole('button', { name: 'Logs' }).first().click();
    const logsOverlay = page.getByTestId('session-logs-overlay');
    const logsPanel = page.getByTestId('session-logs-panel');
    await expect(logsPanel).toHaveAccessibleName('Session logs: fetch');
    expect(await logsOverlay.evaluate((root) => root.parentElement === document.body)).toBe(true);
    expect(await logsOverlay.evaluate((root) => {
      const overlays = [...document.body.querySelectorAll<HTMLElement>('[data-overlay-id]')];
      return overlays[overlays.length - 1] === root;
    })).toBe(true);
    expect(await logsPanel.evaluate((panel) => {
      const rect = panel.getBoundingClientRect();
      const target = document.elementFromPoint(rect.left + rect.width / 2, rect.top + rect.height / 2);
      return Boolean(target?.closest('[data-testid="session-logs-panel"]'));
    })).toBe(true);
    await expect(page.getByTestId('dashboard-entity-modal')).toBeVisible();
    expect(page.url()).toBe(taskModalUrl);
    const logsPath = testInfo.outputPath('control-center-task-step-logs.png');
    await page.screenshot({ path: logsPath, animations: 'disabled' });
    await testInfo.attach('control-center-task-step-logs', { path: logsPath, contentType: 'image/png' });
    await page.keyboard.press('Escape');
    await expect(logsOverlay).toHaveCount(0);
    await expect(page.getByTestId('dashboard-task-modal-panel-steps')).toBeVisible();
    await page.getByTestId('dashboard-task-modal-tab-control-chat').click();
    await expect(page.getByTestId('task-control-native-chat')).toBeVisible();
    await expect(page.getByRole('navigation', { name: 'Primary' })).toHaveCount(0);
    expect(await page.getByRole('dialog').evaluate((dialog) =>
      dialog.parentElement?.parentElement?.parentElement === document.body
    )).toBe(true);
    const dialog = page.getByRole('dialog');
    const dialogHeightBeforeKeyboard = (await dialog.boundingBox())?.height ?? 0;
    const composer = page.getByTestId('task-control-native-chat').locator('textarea').first();
    await composer.focus();
    await page.evaluate(() => {
      const viewport = window.visualViewport;
      if (!viewport) throw new Error('visualViewport is required for the PWA keyboard scenario');
      Object.defineProperty(viewport, 'height', { configurable: true, get: () => 520 });
      Object.defineProperty(viewport, 'offsetTop', { configurable: true, get: () => 0 });
      viewport.dispatchEvent(new Event('resize'));
    });
    await expect.poll(() => page.evaluate(() => document.documentElement.dataset.keyboard))
      .toBe('open');
    await expect.poll(() => page.getByTestId('task-control-native-chat').evaluate((chat) => (
      Number.parseFloat(
        getComputedStyle(chat).getPropertyValue('--app-local-keyboard-occlusion'),
      )
    ))).toBeGreaterThan(250);
    await expect.poll(async () => Math.abs(
      ((await dialog.boundingBox())?.height ?? 0) - dialogHeightBeforeKeyboard,
    )).toBeLessThanOrEqual(1);
    await expect.poll(() => composer.evaluate((textarea) => {
      const box = textarea.getBoundingClientRect();
      const visualBottom = (window.visualViewport?.offsetTop ?? 0)
        + (window.visualViewport?.height ?? window.innerHeight);
      return box.bottom - visualBottom;
    })).toBeLessThanOrEqual(1);
    const composerGeometry = await composer.evaluate((textarea) => {
      const box = textarea.getBoundingClientRect();
      const visualBottom = (window.visualViewport?.offsetTop ?? 0)
        + (window.visualViewport?.height ?? window.innerHeight);
      return {
        composerBottom: box.bottom,
        visualBottom,
        pageOverflow: document.documentElement.scrollHeight - document.documentElement.clientHeight,
        tappable: document.elementFromPoint(
          box.left + Math.min(box.width / 2, 24),
          Math.min(box.bottom - 1, visualBottom - 1),
        ) === textarea,
      };
    });
    expect(composerGeometry.composerBottom).toBeLessThanOrEqual(composerGeometry.visualBottom + 1);
    expect(composerGeometry.pageOverflow).toBeLessThanOrEqual(1);
    expect(composerGeometry.tappable).toBe(true);
    await page.getByTestId('dashboard-task-modal-tab-activity').click();
    await expect(page.getByTestId('task-work-compact')).toBeVisible();
    await expect(page.locator('[data-testid="task-work-compact"] [data-testid="activity-summary-strip"]')).toBeVisible();
    cockpitFixture.setStatus('completed');
    await page.getByTestId('dashboard-task-modal-tab-deliverable').click();
    await expect(page.getByTestId('task-final-result')).toBeVisible();
    await expect(page.getByTestId('rich-deliverable-inline-document')).toBeVisible();
    await expect(page.getByRole('button', { name: 'Expand document' })).toHaveCount(0);
    const taskPath = testInfo.outputPath('control-center-pwa-task-modal.png');
    await page.screenshot({ path: taskPath, animations: 'disabled' });
    await testInfo.attach('control-center-pwa-task-modal', { path: taskPath, contentType: 'image/png' });
    await page.getByTestId('dashboard-entity-modal-close').click();

    await page.setViewportSize({ width: 1440, height: 1000 });
    await page.reload();
    if (await page.getByTestId('workspace-layer').count() === 0) await page.reload();
    await expect(page.getByTestId('workspace-layer')).toBeVisible();
    await page.getByTestId('dashboard-schedule-row-schedule-pwa').click();
    const scheduleWindow = page.getByTestId('workspace-window-schedule:schedule-pwa');
    await expect(scheduleWindow).toBeVisible();
    await expect(scheduleWindow.getByTestId('dashboard-schedule-modal-tab-description')).toBeVisible();
    await expect(scheduleWindow.getByTestId('dashboard-schedule-modal-tab-chat')).toHaveCount(0);
    await expect(scheduleWindow.getByLabel('Open full page')).toHaveAttribute(
      'href',
      '/schedules/schedule-pwa'
    );
    await expect(scheduleWindow.getByLabel('Minimize')).toBeVisible();
    await expect(scheduleWindow.getByLabel('Close')).toBeVisible();
    await scheduleWindow.getByTestId('dashboard-schedule-modal-tab-steps').click();
    await expect(scheduleWindow.getByTestId('dashboard-schedule-planned-steps')).toContainText(
      'Prepare evidence'
    );
    const schedulePath = testInfo.outputPath('control-center-pwa-schedule-window.png');
    await page.screenshot({ path: schedulePath, animations: 'disabled' });
    await testInfo.attach('control-center-pwa-schedule-window', {
      path: schedulePath,
      contentType: 'image/png'
    });
    await page.getByTestId('workspace-window-minimize-schedule:schedule-pwa').click();
    await expect(scheduleWindow).toHaveAttribute('data-minimized', 'true');
    await page.getByTestId('workspace-dock-restore-schedule:schedule-pwa').click();
    await expect(scheduleWindow).toHaveAttribute('data-minimized', 'false');

    await page.setViewportSize({ width: 390, height: 844 });
    await page.reload();
    await expect(page.locator('[data-testid^="workspace-window-"][role="dialog"]')).toHaveCount(0);
    const appScroller = page.locator('[data-app-content="true"]');
    await page.getByTestId('dashboard-schedule-row-schedule-pwa').scrollIntoViewIfNeeded();
    const backgroundBefore = await appScroller.evaluate((element) => element.scrollTop);
    await page.getByTestId('dashboard-schedule-row-schedule-pwa').click();
    const blockingSchedule = page.locator(
      '[data-blocking-overlay][role="dialog"][aria-label="Daily release review"]',
    );
    await expect(blockingSchedule.getByTestId('dashboard-schedule-modal-tab-description')).toBeVisible();
    await expect(blockingSchedule).toBeVisible();
    await expect(page.getByRole('navigation', { name: 'Primary' })).toHaveCount(0);
    await expect(page.getByTestId('dashboard-schedule-modal-close')).toBeVisible();
    const scheduleBox = await page.getByRole('dialog').boundingBox();
    await page.getByTestId('dashboard-schedule-modal-close').click();
    await expect.poll(() => appScroller.evaluate((element) => element.scrollTop)).toBe(backgroundBefore);

    const agentButton = page.locator('[data-testid^="dashboard-agent-avatar-"]').first();
    await expect(agentButton).toBeVisible();
    await agentButton.click();
    await expect(page.getByTestId('agent-quick-chat-modal-close')).toBeVisible();
    await expect(page.getByTestId('compact-conversation-chat')).toBeVisible();
    const agentChat = page.getByTestId('compact-conversation-chat');
    const agentViewport = agentChat.getByTestId('scoped-timeline-viewport');
    await expect(agentChat).toHaveAttribute('data-embedded', 'true');
    await expect(agentChat.getByText('Loading timeline…')).toBeHidden({ timeout: 30_000 });
    await expect(agentViewport.locator('[data-timeline-row-key]')).toHaveCount(12);
    await expect.poll(() => agentViewport.evaluate((node) => {
      const element = node as HTMLElement;
      return {
        overflow: element.scrollHeight > element.clientHeight,
        atTail: element.scrollTop + element.clientHeight >= element.scrollHeight - 2
      };
    })).toEqual({ overflow: true, atTail: true });
    await expect(agentChat).toHaveAttribute('data-auto-tail', 'following');
    const commandResponse = page.waitForResponse((response) =>
      response.request().method() === 'PUT'
      && /\/api\/v1\/chat\/v2\/conversations\/conv-task-chat\/commands\/[^/]+$/.test(new URL(response.url()).pathname)
    );
    const agentComposer = agentChat.getByTestId('compact-chat-composer');
    await agentComposer.fill('/help');
    await agentComposer.press('Enter');
    expect((await commandResponse).status()).toBe(200);
    const commandRequests = cockpitFixture.commandRequests();
    expect(commandRequests).toHaveLength(1);
    expect(commandRequests[0]).toMatchObject({
      method: 'PUT',
      conversationId: 'conv-task-chat',
      body: { content: '/help' }
    });
    expect(commandRequests[0]?.path).toBe(
      `/api/v1/chat/v2/conversations/conv-task-chat/commands/${commandRequests[0]?.clientTxnId}`
    );
    expect(commandRequests[0]?.clientTxnId).toMatch(
      /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i
    );
    await expect(agentComposer).toHaveValue('');
    await expect(agentChat.getByTestId('compact-command-result')).toHaveText(
      'Available commands: /help, /new, /model, /profile.'
    );
    await expect(agentChat.getByText(/Unmocked cockpit fixture API/i)).toHaveCount(0);
    await expect(agentChat.getByText(/not yet available/i)).toHaveCount(0);
    await expect(agentChat.getByText(/Could not (execute|send)/i)).toHaveCount(0);
    await expect(agentChat.getByText(/retry/i)).toHaveCount(0);
    await expect(agentChat.getByRole('alert')).toHaveCount(0);
    expect(cockpitFixture.unmockedRequests().filter((request) => request.includes('/commands/'))).toEqual([]);
    const duplicateCommand = await page.evaluate(async ({ path, body }) => {
      const response = await fetch(path, {
        method: 'PUT',
        credentials: 'include',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body)
      });
      return { status: response.status, body: await response.json() };
    }, {
      path: commandRequests[0]!.path,
      body: commandRequests[0]!.body
    });
    expect(duplicateCommand).toMatchObject({
      status: 200,
      body: {
        conversation_id: 'conv-task-chat',
        client_txn_id: commandRequests[0]!.clientTxnId,
        status: 'duplicate',
        text: 'Available commands: /help, /new, /model, /profile.'
      }
    });
    expect(cockpitFixture.commandRequests()).toHaveLength(2);
    for (const [directive, expectedContent, expectedMode] of [
      ['/plan investigate', 'investigate', 'plan'],
      ['/build implement', 'implement', 'build']
    ] as const) {
      const response = page.waitForResponse((candidate) => (
        candidate.request().method() === 'PUT'
        && /\/api\/v1\/chat\/v2\/conversations\/conv-task-chat\/messages\/[^/]+$/.test(
          new URL(candidate.url()).pathname
        )
      ));
      await agentComposer.fill(directive);
      await agentComposer.press('Control+Enter');
      expect((await response).status()).toBe(202);
      await expect(agentComposer).toHaveValue('');
      const messageRequests = cockpitFixture.messageRequests();
      const request = messageRequests[messageRequests.length - 1];
      expect(request?.body).toMatchObject({
        content: expectedContent,
        chat_mode: expectedMode
      });
      await expect(agentChat.getByText(expectedContent, { exact: true })).toBeVisible();
    }
    const barePlanResponse = page.waitForResponse((response) => (
      response.request().method() === 'PUT'
      && /\/api\/v1\/chat\/v2\/conversations\/conv-task-chat\/commands\/[^/]+$/.test(
        new URL(response.url()).pathname
      )
    ));
    await agentComposer.fill('/plan');
    await agentComposer.press('Control+Enter');
    expect((await barePlanResponse).status()).toBe(200);
    const planCommandRequests = cockpitFixture.commandRequests();
    expect(planCommandRequests[planCommandRequests.length - 1]?.body).toEqual({ content: '/plan' });
    expect(
      cockpitFixture.messageRequests().some((request) => request.body.content === '/plan')
    ).toBe(false);
    await expect(agentChat.getByTestId('compact-command-result')).toBeVisible();
    const agentBox = await page.getByRole('dialog').boundingBox();
    expect(await page.getByRole('dialog').evaluate((dialog) =>
      dialog.parentElement?.parentElement?.parentElement === document.body
    )).toBe(true);
    expect(agentBox?.width).toBe(scheduleBox?.width);
    expect(agentBox?.height).toBe(scheduleBox?.height);
    const agentPath = testInfo.outputPath('control-center-pwa-agent-modal.png');
    await page.screenshot({ path: agentPath, animations: 'disabled' });
    await testInfo.attach('control-center-pwa-agent-modal', { path: agentPath, contentType: 'image/png' });
    await agentComposer.fill('Append a deterministic tail event.');
    await agentComposer.press('Control+Enter');
    await expect(agentComposer).toHaveValue('');
    await expect(agentChat.getByText('Append a deterministic tail event.', { exact: true })).toBeVisible();
    await expect(agentViewport.locator('[data-timeline-row-key]')).toHaveCount(15);
    await expect.poll(() => agentViewport.evaluate((node) => {
      const element = node as HTMLElement;
      return element.scrollTop + element.clientHeight >= element.scrollHeight - 2;
    })).toBe(true);
    await expect(agentChat).toHaveAttribute('data-auto-tail', 'following');
    await agentViewport.hover();
    await page.mouse.wheel(0, -600);
    await expect(agentChat).toHaveAttribute('data-auto-tail', 'paused');
    await expect.poll(() => agentViewport.evaluate((node) => {
      const element = node as HTMLElement;
      return element.scrollTop + element.clientHeight < element.scrollHeight - 24;
    })).toBe(true);
  });

  test('keeps the dashboard viewport-bounded with fixed chrome and independently scrolling list bodies', async ({ page }) => {
    const runningTask = (index: number) => ({
      task_id: `running-${index}`,
      title: `Running task number ${index} with a deliberately long title to exercise truncation`,
      status: 'running',
      priority: 3,
      agent_id: 'riker',
      created_at: '2026-08-24T08:00:00Z',
      started_at: '2026-08-24T08:01:00Z',
      completed_at: null,
      updated_at: '2026-08-24T08:02:00Z',
      progress_summary: null
    });
    const doneTask = (index: number) => ({
      task_id: `recent-${index}`,
      title: `Recent task number ${index}`,
      status: 'completed',
      priority: 3,
      agent_id: 'riker',
      created_at: '2026-08-24T06:00:00Z',
      started_at: '2026-08-24T06:01:00Z',
      completed_at: '2026-08-24T07:00:00Z',
      updated_at: '2026-08-24T07:00:00Z',
      progress_summary: null
    });
    const pausedTask = (index: number) => ({
      task_id: `waiting-${index}`,
      title: `Waiting task number ${index}`,
      status: 'paused',
      attention_type: 'gate',
      priority: 3,
      agent_id: 'riker',
      created_at: '2026-08-24T05:00:00Z',
      started_at: '2026-08-24T05:01:00Z',
      completed_at: null,
      updated_at: '2026-08-24T05:30:00Z',
      progress_summary: null
    });

    await page.route('**/api/v1/tasks/board**', async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          columns: {
            running: { total_count: 5, items: Array.from({ length: 5 }, (_, index) => runningTask(index)) },
            paused: { total_count: 5, items: Array.from({ length: 5 }, (_, index) => pausedTask(index)) },
            done: { total_count: 5, items: Array.from({ length: 5 }, (_, index) => doneTask(index)) }
          }
        })
      });
    });
    await page.route('**/api/v1/schedules**', async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(Array.from({ length: 5 }, (_, index) => ({
          schedule_id: `schedule-${index}`,
          name: `Daily schedule number ${index}`,
          agent_id: 'riker',
          enabled: true,
          next_fire_at: `2026-08-2${index}T08:00:00Z`
        })))
      });
    });
    await page.route('**/api/v1/dashboard/issues', async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          generated_at: '2026-08-24T08:00:00Z',
          summary: { total: 8, critical: 2, warning: 6, info: 0, truncated: false },
          issues: Array.from({ length: 8 }, (_, index) => ({
            id: `issue-${index}`,
            severity: index < 2 ? 'critical' : 'warning',
            kind: 'executor_degraded',
            title: `System issue number ${index}`,
            detail: 'A bounded issue row used to exercise scroll containment.',
            resource: { type: 'executor', id: `executor-${index}`, label: `Executor ${index}` },
            observed_at: '2026-08-24T08:00:00Z',
            action_url: '/settings'
          }))
        })
      });
    });
    await page.route('**/api/v1/conversations?**', async (route) => {
      const conversation = (id: string, overrides: Record<string, unknown>) => ({
        conversation_id: id,
        user_email: 'admin@cognis-e2e.localdev.me',
        agent_id: 'riker',
        agent_profile_id: null,
        project_id: null,
        title: `Conversation ${id}`,
        title_source: 'user',
        context: { type: 'web', ref: `web:topic:${id}`, platform_data: { kind: 'topic' }, memory_labels: {} },
        active_session_id: null,
        active_session_status: null,
        active_session_completion_reason: null,
        pending_notification_types: [],
        starred_at: null,
        status: 'active',
        last_message_at: '2026-08-24T08:00:00Z',
        last_read_at: null,
        has_unread: false,
        has_active_turn: false,
        managed_agent: null,
        root_controller_conversation_id: null,
        created_at: '2026-08-24T08:00:00Z',
        updated_at: '2026-08-24T08:00:00Z',
        conversation_state: null,
        ...overrides
      });
      const active = Array.from({ length: 5 }, (_, index) => (
        conversation(`active-${index}`, { has_active_turn: true })
      ));
      const waiting = Array.from({ length: 5 }, (_, index) => (
        conversation(`waiting-${index}`, { pending_notification_types: ['gate'] })
      ));
      const recent = Array.from({ length: 8 }, (_, index) => conversation(`recent-${index}`, {}));
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ items: [...active, ...waiting, ...recent], cursor: null, has_more: false })
      });
    });

    await login(page);

    for (const { width, height } of [
      { width: 820, height: 1180 },
      { width: 1280, height: 720 },
      { width: 1440, height: 900 },
      { width: 1920, height: 1080 }
    ]) {
      await page.setViewportSize({ width, height });
      await page.goto('/');
      await expect(page.getByTestId('dashboard-tasks-section')).toBeVisible();
      await expect(page.getByTestId('dashboard-conversations-section')).toBeVisible();
      await expect(page.getByTestId('dashboard-issues-list')).toBeVisible();

      // The page-level scroll surface and the dashboard root must not need
      // to scroll vertically: fixed chrome plus bounded list bodies fill
      // exactly the available viewport height.
      await expect.poll(() => page.evaluate(() => {
        const app = document.querySelector<HTMLElement>('[data-app-content="true"]');
        if (!app) return null;
        return app.scrollHeight - app.clientHeight;
      })).toBeLessThanOrEqual(1);
      const dashboardOverflow = await page.evaluate(() => {
        const root = document.querySelector<HTMLElement>('[data-testid="control-center"]');
        if (!root) return null;
        return root.scrollHeight - root.clientHeight;
      });
      expect(dashboardOverflow).toBeLessThanOrEqual(1);

      // Section chrome (headings, new-task/new-chat buttons) is fixed;
      // only bounded list bodies scroll. The Recent task list and the
      // issues list both overflow their bounded regions here, proving
      // list-body scrolling is independent of page/card scrolling.
      const tasksHeading = page.getByTestId('dashboard-tasks-section').getByRole('heading', { name: 'Tasks' });
      const issuesHeadline = page.getByTestId('dashboard-issues-headline');
      await expect(tasksHeading).toBeVisible();
      await expect(issuesHeadline).toBeVisible();

      const recentList = page.getByTestId('dashboard-tasks-recent-list');
      await expect(recentList).toBeVisible();
      await expect(recentList.locator('[data-testid^="dashboard-task-row-"]').first()).toBeVisible();
      const recentMetrics = await recentList.evaluate((node) => ({
        clientHeight: node.clientHeight,
        overflow: node.scrollHeight - node.clientHeight
      }));
      expect(recentMetrics.clientHeight).toBeGreaterThan(20);
      expect(recentMetrics.overflow).toBeGreaterThan(0);
      const tasksHeadingBoxBefore = await tasksHeading.boundingBox();
      await recentList.evaluate((node) => { node.scrollTop = node.scrollHeight; });
      await expect.poll(() => recentList.evaluate((node) => node.scrollTop)).toBeGreaterThan(0);
      await expect(tasksHeading).toBeVisible();
      const tasksHeadingBoxAfter = await tasksHeading.boundingBox();
      expect(tasksHeadingBoxAfter?.y).toBe(tasksHeadingBoxBefore?.y);

      for (const testId of [
        'dashboard-tasks-running-list',
        'dashboard-tasks-waiting-list',
        'dashboard-tasks-upcoming-list',
        'dashboard-conversations-active-list',
        'dashboard-conversations-waiting-list'
      ]) {
        const upperList = page.getByTestId(testId);
        await expect(upperList).toBeVisible();
        await expect(upperList.locator('li').first()).toBeVisible();
        const upperMetrics = await upperList.evaluate((node) => ({
          clientHeight: node.clientHeight,
          overflow: node.scrollHeight - node.clientHeight
        }));
        expect(upperMetrics.clientHeight, `${testId} at ${width}x${height}`).toBeGreaterThan(0);
        expect(upperMetrics.overflow).toBeGreaterThan(0);
        await upperList.evaluate((node) => { node.scrollTop = node.scrollHeight; });
        await expect.poll(() => upperList.evaluate((node) => node.scrollTop)).toBeGreaterThan(0);
      }

      const issuesList = page.getByTestId('dashboard-issues-list');
      await expect(issuesList).toBeVisible();
      await expect(issuesList.locator('[data-testid^="dashboard-issue-"]').first()).toBeVisible();
      const issuesMetrics = await issuesList.evaluate((node) => ({
        clientHeight: node.clientHeight,
        overflow: node.scrollHeight - node.clientHeight
      }));
      expect(issuesMetrics.clientHeight).toBeGreaterThan(20);
      expect(issuesMetrics.overflow).toBeGreaterThan(0);
      const issuesHeadlineBoxBefore = await issuesHeadline.boundingBox();
      await issuesList.evaluate((node) => { node.scrollTop = node.scrollHeight; });
      await expect.poll(() => issuesList.evaluate((node) => node.scrollTop)).toBeGreaterThan(0);
      await expect(issuesHeadline).toBeVisible();
      const issuesHeadlineBoxAfter = await issuesHeadline.boundingBox();
      expect(issuesHeadlineBoxAfter?.y).toBe(issuesHeadlineBoxBefore?.y);
    }
  });

  test('keeps the Recent lane reachable when upper lanes are all fully populated', async ({ page }) => {
    // Regression for lane starvation: five running + five waiting tasks
    // (each individually bounded and scrollable) must not consume so much
    // height that the Recent lane collapses to zero and becomes
    // unreachable. Upper lanes should shrink first; Recent keeps a usable,
    // scrollable slice of the remaining height.
    await page.route('**/api/v1/tasks/board**', async (route) => {
      const task = (prefix: string, index: number, overrides: Record<string, unknown>) => ({
        task_id: `${prefix}-${index}`,
        title: `${prefix} task ${index}`,
        status: 'running',
        priority: 3,
        agent_id: 'riker',
        created_at: '2026-08-24T05:00:00Z',
        started_at: '2026-08-24T05:01:00Z',
        completed_at: null,
        updated_at: '2026-08-24T05:30:00Z',
        progress_summary: null,
        ...overrides
      });
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          columns: {
            running: {
              total_count: 5,
              items: Array.from({ length: 5 }, (_, index) => task('running', index, {}))
            },
            paused: {
              total_count: 5,
              items: Array.from({ length: 5 }, (_, index) => (
                task('waiting', index, { status: 'paused', attention_type: 'gate' })
              ))
            },
            done: {
              total_count: 5,
              items: Array.from({ length: 5 }, (_, index) => (
                task('recent', index, { status: 'completed', completed_at: '2026-08-24T07:00:00Z' })
              ))
            }
          }
        })
      });
    });

    await login(page);
    await page.setViewportSize({ width: 1280, height: 720 });
    await page.goto('/');

    const recentSection = page.getByTestId('dashboard-tasks-recent');
    await expect(recentSection).toBeVisible();
    await expect(recentSection.getByText('Recent', { exact: true })).toBeVisible();
    const recentList = page.getByTestId('dashboard-tasks-recent-list');
    await expect(recentList).toBeVisible();
    const recentBox = await recentList.boundingBox();
    expect(recentBox?.height ?? 0).toBeGreaterThan(20);
    await expect(recentList.getByTestId('dashboard-task-row-recent-0')).toBeVisible();

    // The card itself must still fit inside the viewport-bounded layout.
    await expect.poll(() => page.evaluate(() => {
      const app = document.querySelector<HTMLElement>('[data-app-content="true"]');
      return app ? app.scrollHeight - app.clientHeight : null;
    })).toBeLessThanOrEqual(1);
  });

  test('remains scroll-free with zero and with a single issue', async ({ page }) => {
    await page.route('**/api/v1/tasks/board**', async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          columns: {
            running: { total_count: 0, items: [] },
            done: { total_count: 0, items: [] }
          }
        })
      });
    });
    await page.route('**/api/v1/dashboard/issues', async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          generated_at: '2026-08-24T08:00:00Z',
          summary: { total: 0, critical: 0, warning: 0, info: 0, truncated: false },
          issues: []
        })
      });
    });

    await login(page);
    await page.setViewportSize({ width: 1440, height: 900 });
    await page.goto('/');
    await expect(page.getByTestId('dashboard-issues-strip')).toHaveCount(0);
    await expect(page.getByTestId('dashboard-tasks-section')).toBeVisible();
    await expect.poll(() => page.evaluate(() => {
      const app = document.querySelector<HTMLElement>('[data-app-content="true"]');
      return app ? app.scrollHeight - app.clientHeight : null;
    })).toBeLessThanOrEqual(1);

    await page.route('**/api/v1/dashboard/issues', async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          generated_at: '2026-08-24T08:00:00Z',
          summary: { total: 1, critical: 0, warning: 1, info: 0, truncated: false },
          issues: [{
            id: 'single-issue',
            severity: 'warning',
            kind: 'executor_degraded',
            title: 'A single actionable issue',
            detail: 'Only one issue is present.',
            resource: { type: 'executor', id: 'executor-1', label: 'Executor one' },
            observed_at: '2026-08-24T08:00:00Z',
            action_url: '/settings'
          }]
        })
      });
    });
    await page.goto('/');
    await expect(page.getByTestId('dashboard-issue-single-issue')).toBeVisible();
    await expect.poll(() => page.evaluate(() => {
      const app = document.querySelector<HTMLElement>('[data-app-content="true"]');
      return app ? app.scrollHeight - app.clientHeight : null;
    })).toBeLessThanOrEqual(1);
  });
});
