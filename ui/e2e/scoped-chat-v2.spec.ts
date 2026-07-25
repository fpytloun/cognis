import { test, expect } from '@playwright/test';

test.describe('ScopedChatV2Timeline', () => {
  test.use({ serviceWorkers: 'block' });

  test.beforeEach(async ({ page }) => {
    await page.goto('/scoped-chat-v2-fixture');
    await expect(page.getByTestId('scoped-timeline-shell')).toBeVisible();
  });

  test('renders a task-step scope through the production timeline', async ({ page }) => {
    await page.getByTestId('scope-task-step').click();
    await expect(page.getByTestId('active-scope')).toContainText('Task step');
    await expect(page.locator('[data-scope-key="task_step:fixture-step"]')).toBeVisible();
    await expect(page.getByText('Task step event 20', { exact: false }).first()).toBeVisible();
    await expect(page.getByRole('heading', { name: 'Task step event 20' })).toBeVisible();
    await expect(page.getByTestId('scoped-timeline-shell').locator('.chat-markdown a[href^="javascript:"]')).toHaveCount(0);
    await expect(page.getByTestId('scoped-timeline-shell').locator('[data-kind="todo_state"]')).toHaveCount(0);
    await expect(page.getByTestId('scoped-timeline-shell').locator('[data-timeline-row-key="todo_state:task_step:fixture-step:visual-todo"]')).toHaveCount(0);
    await expect(page.getByTestId('scoped-timeline-shell').getByRole('button', { name: /Todos/ })).toBeVisible();
  });

  test('uses regular-chat viewport density and canonical row grouping across scopes', async ({ page }) => {
    const shell = page.getByTestId('scoped-timeline-shell');
    const viewport = shell.getByTestId('scoped-timeline-viewport');
    const content = shell.getByTestId('scoped-timeline-viewport-content');
    await expect(viewport).toHaveClass(/px-2\.5/);
    await expect(content).toHaveClass(/space-y-3/);

    const signature = async () => shell.locator('[data-timeline-row-key]').evaluateAll((rows) =>
      rows.map((row) => ({
        kind: row.querySelector('[data-kind]')?.getAttribute('data-kind') ?? '',
        className: row.className,
      }))
    );
    const parent = await signature();
    await page.getByTestId('scope-child').click();
    await expect(shell.getByText('Child delegated session event 20', { exact: true })).toBeVisible();
    const child = await signature();
    await page.getByTestId('scope-task-step').click();
    await expect(shell.getByText('Task step event 20', { exact: true })).toBeVisible();
    const taskStep = await signature();

    expect(child.map((row) => row.kind)).toEqual(parent.map((row) => row.kind));
    expect(taskStep.map((row) => row.kind)).toEqual(parent.map((row) => row.kind));
    expect(child.map((row) => row.className)).toEqual(parent.map((row) => row.className));
    expect(taskStep.map((row) => row.className)).toEqual(parent.map((row) => row.className));
  });

  test('pins a running scope to the true tail, pauses on trusted scroll, and resumes follow', async ({ page }) => {
    const shell = page.getByTestId('scoped-timeline-shell');
    const viewport = shell.getByTestId('scoped-timeline-viewport');
    await expect.poll(() => viewport.evaluate((node) => {
      const element = node as HTMLElement;
      return Math.round(element.scrollHeight - element.scrollTop - element.clientHeight);
    })).toBeLessThanOrEqual(2);

    await viewport.hover();
    await page.mouse.wheel(0, -500);
    await expect(shell.getByRole('button', { name: 'Resume live follow' })).toBeVisible();
    const pausedTop = await viewport.evaluate((node) => (node as HTMLElement).scrollTop);
    await page.getByTestId('emit-active-frame').click();
    await expect(shell.getByText('Parent conversation live frame')).toBeVisible();
    expect(await viewport.evaluate((node) => (node as HTMLElement).scrollTop)).toBe(pausedTop);

    await shell.getByRole('button', { name: 'Resume live follow' }).click();
    await expect.poll(() => viewport.evaluate((node) => {
      const element = node as HTMLElement;
      return Math.round(element.scrollHeight - element.scrollTop - element.clientHeight);
    })).toBeLessThanOrEqual(2);
  });

  test('updates task evaluation and outcome without resetting scope and excludes it from nested sessions', async ({ page }) => {
    await page.route('**/api/v1/step-runs/fixture-step/deliverables/fixture-deliverable', async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          deliverable_id: 'fixture-deliverable',
          step_run_id: 'fixture-step',
          version: 2,
          attempt_number: 1,
          content: 'Fixture fallback',
          format: 'rich',
          title: 'Fixture outcome',
          target: null,
          outputs: {},
          rich_payload: { blocks: [{ type: 'markdown', content: '# Fixture rich outcome' }] },
          status: 'approved',
          evaluator_feedback: 'Looks correct',
          created_at: '2026-01-01T00:02:00Z',
          updated_at: '2026-01-01T00:02:00Z',
        }),
      });
    });
    await page.getByTestId('scope-task-step').click();
    const shell = page.getByTestId('scoped-timeline-shell');
    const scopeNode = shell.locator('[data-scope-key="task_step:fixture-step"]');
    await expect(scopeNode).toHaveAttribute('data-cursor', /cursor/);
    const cursor = await scopeNode.getAttribute('data-cursor');
    await page.getByTestId('set-evaluating').click();
    await expect(shell.getByText('Evaluator is reviewing this attempt…')).toBeVisible();
    await expect(scopeNode).toHaveAttribute('data-cursor', cursor ?? '');
    await page.getByTestId('set-approved').click();
    await expect(shell.getByTestId('task-step-outcome')).toContainText('approved');
    await expect(shell.getByText('Fixture rich outcome')).toBeVisible();
    await expect(shell.getByText('Verified outcome')).toBeVisible();
    await expect(scopeNode).toHaveAttribute('data-cursor', cursor ?? '');

    await page.getByTestId('scope-child').click();
    await expect(shell.getByTestId('task-step-outcome')).toHaveCount(0);
  });

  test('authoritative terminal task metadata suppresses a stale active runtime without resetting scope or scroll', async ({ page }) => {
    await page.getByTestId('scope-task-step').click();
    const shell = page.getByTestId('scoped-timeline-shell');
    const scopeNode = shell.locator('[data-scope-key="task_step:fixture-step"]');
    const viewport = shell.getByTestId('scoped-timeline-viewport');
    await expect(scopeNode).toHaveAttribute('data-cursor', /cursor/);
    await page.getByTestId('emit-active-frame').click();
    await expect(shell.getByRole('navigation', { name: 'Live tail controls' })).toBeVisible();
    const cursor = await scopeNode.getAttribute('data-cursor');
    await viewport.hover();
    await page.mouse.wheel(0, -500);
    await expect(shell.getByRole('button', { name: 'Resume live follow' })).toBeVisible();
    const scrollTop = await viewport.evaluate((node) => (node as HTMLElement).scrollTop);

    await page.getByTestId('set-failed').click();

    await expect(shell.getByTestId('task-step-outcome')).toContainText('failed');
    await expect(shell.getByRole('navigation', { name: 'Live tail controls' })).toHaveCount(0);
    await expect(scopeNode).toHaveAttribute('data-cursor', cursor ?? '');
    expect(await viewport.evaluate((node) => (node as HTMLElement).scrollTop)).toBe(scrollTop);
  });

  test('generic session scope still uses runtime-only activity', async ({ page }) => {
    await page.getByTestId('scope-child').click();
    const shell = page.getByTestId('scoped-timeline-shell');
    await page.getByTestId('emit-active-frame').click();
    await expect(shell.getByRole('navigation', { name: 'Live tail controls' })).toBeVisible();
  });

  test('loads full tool output through the exact task-step scope endpoint', async ({ page }) => {
    let requestedUrl = '';
    await page.route('**/api/v1/chat/v2/task-steps/fixture-step/tool-outputs/call_fixture_3**', async (route) => {
      requestedUrl = route.request().url();
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          conversation_id: 'fixture-conversation',
          session_id: 'fixture-step-session',
          call_id: 'call_fixture_3',
          status: 'completed',
          source: 'stored_output',
          content: 'historical task-step full output',
          chunks: [],
          offset: 1,
          limit: 200,
          has_more_before: false,
          has_more_after: false,
          output_size: 32,
          recoverable: true,
          truncated: false,
          spool_truncated: false,
        }),
      });
    });
    await page.getByTestId('scope-task-step').click();
    const timeline = page.getByTestId('scoped-timeline-shell');
    const collapsed = timeline.locator('[data-kind="tool_group"] > button[aria-expanded="false"]');
    while (await collapsed.count()) await collapsed.first().click();
    const webTool = timeline.locator('[data-kind="tool_call"]').filter({ hasText: /web_search canonical Chat v2/i }).first();
    await webTool.getByRole('button', { name: /web_search canonical Chat v2/i }).click();
    await webTool.getByRole('button', { name: /Open full output/i }).click();
    await expect(timeline.getByText('historical task-step full output')).toBeVisible();
    expect(requestedUrl).toContain('/api/v1/chat/v2/task-steps/fixture-step/tool-outputs/call_fixture_3');
    expect(requestedUrl).not.toContain('session_id=');
  });

  test('renders sanitized semantic Markdown on conversation and nested-session surfaces', async ({ page }) => {
    const timeline = page.getByTestId('scoped-timeline-shell');
    await expect(timeline.getByRole('heading', { name: 'Parent conversation event 20' })).toBeVisible();
    await expect(timeline.locator('.chat-markdown a[href^="javascript:"]')).toHaveCount(0);
    await page.getByTestId('scope-grandchild').click();
    await expect(timeline.getByRole('heading', { name: 'Grandchild delegated session event 20' })).toBeVisible();
    await expect(timeline.getByText('Scoped markdown').last()).toBeVisible();
    await expect(timeline.locator('.chat-markdown a[href^="javascript:"]')).toHaveCount(0);
  });

  test('keeps parent, child, and grandchild lineage isolated while frames are active', async ({ page }) => {
    const timeline = page.getByTestId('scoped-timeline-shell');
    await expect(page.getByTestId('active-scope')).toContainText('Parent');
    await page.getByTestId('emit-active-frame').click();
    await expect(timeline.getByText('Parent conversation live frame')).toBeVisible();

    await page.getByTestId('scope-child').click();
    await expect(page.getByTestId('active-scope')).toContainText('Child');
    await page.getByTestId('emit-active-frame').click();
    await expect(timeline.getByText('Child delegated session live frame')).toBeVisible();
    await expect(timeline.getByText('Parent conversation live frame')).toHaveCount(0);

    await page.getByTestId('scope-grandchild').click();
    await page.getByTestId('emit-active-frame').click();
    await expect(timeline.getByText('Grandchild delegated session live frame')).toBeVisible();
    await page.getByTestId('back-parent').click();
    await expect(page.getByTestId('active-scope')).toContainText('Child');
    await expect(timeline.getByText('Child delegated session event').first()).toBeVisible();
    await expect(timeline.getByText('Grandchild delegated session live frame')).toHaveCount(0);
    await page.getByTestId('back-parent').click();
    await expect(page.getByTestId('active-scope')).toContainText('Parent');
  });

  test('shows an explicit missing_stream state', async ({ page }) => {
    await page.getByTestId('scope-missing').click();
    const timeline = page.getByTestId('scoped-timeline-shell');
    await expect(timeline.getByText('This stream is not available yet.')).toBeVisible();
    await expect(timeline.locator('[data-has-older="true"]')).toHaveCount(0);
    await page.getByTestId('emit-active-frame').click();
    await expect(timeline.getByText('Missing task stream live frame')).toHaveCount(0);
    await expect.poll(() => page.evaluate(() => {
      const controller = (window as typeof window & {
        __scopedFixtureController?: { operationLog: string[] };
      }).__scopedFixtureController;
      return controller?.operationLog ?? [];
    })).not.toEqual(expect.arrayContaining([expect.stringMatching(/^subscribe:task_step:missing-step:/)]));
  });

  test('recovers after a cursor reset and renders the fresh canonical snapshot', async ({ page }) => {
    const timeline = page.getByTestId('scoped-timeline-shell');
    const before = timeline.locator('[data-cursor]');
    await expect(before).toHaveAttribute('data-cursor', /fixture-conversation/);
    await page.getByTestId('reset-cursor').click();
    await expect(before).toHaveAttribute('data-cursor', /-1$/);
    await expect(timeline.getByText('Parent conversation event 20')).toBeVisible();
  });

  test('loads multiple scoped history pages, preserves the scroll anchor, and reaches terminal cursor', async ({ page }) => {
    const timeline = page.getByTestId('scoped-timeline-shell');
    const viewport = timeline.getByTestId('scoped-timeline-viewport');
    await expect(timeline.getByRole('button', { name: /Load older/ })).toBeVisible();
    const anchor = await viewport.evaluate((node) => {
      const element = node as HTMLElement;
      element.scrollTop = 120;
      element.dispatchEvent(new Event('scroll'));
      return element.scrollHeight - element.scrollTop;
    });
    await expect(timeline.getByRole('button', { name: 'Resume live follow' })).toBeVisible();
    const settledAnchor = await viewport.evaluate((node) => {
      const element = node as HTMLElement;
      return element.scrollHeight - element.scrollTop;
    });
    await timeline.getByRole('button', { name: /Load older/ }).click();
    await expect(timeline.locator('[data-has-older="true"]')).toBeVisible();
    await page.waitForTimeout(50);
    const after = await viewport.evaluate((node) => {
      const element = node as HTMLElement;
      return element.scrollHeight - element.scrollTop;
    });
    expect(anchor).toBeGreaterThan(0);
    expect(Math.abs(after - settledAnchor)).toBeLessThanOrEqual(2);
    await expect(timeline.getByText('Parent conversation event 4', { exact: true })).toBeVisible();
    await timeline.getByRole('button', { name: /Load older/ }).click();
    await expect(timeline.locator('[data-has-older="false"]')).toBeVisible();
    await expect(timeline.getByRole('button', { name: /Load older/ })).toHaveCount(0);
    await expect(timeline.getByText('Parent conversation event 4', { exact: true })).toBeVisible();
    await expect.poll(() => page.evaluate(() => {
      const controller = (window as typeof window & {
        __scopedFixtureController?: { operationLog: string[] };
      }).__scopedFixtureController;
      return controller?.operationLog ?? [];
    })).toEqual(expect.arrayContaining([
      expect.stringMatching(/^subscribe:conversation:fixture-conversation:fixture-conversation:/),
    ]));
    await expect.poll(() => page.evaluate(async () => {
      const controller = (window as typeof window & {
        __scopedFixtureController?: { api: { timeline: (scope: unknown, options: { before: string }) => Promise<unknown> } };
      }).__scopedFixtureController;
      if (!controller) return false;
      try {
        await controller.api.timeline({ key: 'session:fixture-child', kind: 'session', session_id: 'fixture-child' }, { before: 'fixture-conversation:fixture-conversation-cursor-0:page:1' });
        return false;
      } catch {
        return true;
      }
    })).toBe(true);
  });

  test('accepts a valid frame after the production refresh button and rejects stale or cross-scope frames', async ({ page }) => {
    const timeline = page.getByTestId('scoped-chat-v2-fixture').getByTestId('scoped-timeline-shell');
    await timeline.getByRole('button', { name: 'Refresh timeline' }).click();
    await expect(timeline.locator('[data-cursor]')).toHaveAttribute('data-cursor', /fixture-conversation/);
    await page.getByTestId('emit-active-frame').click();
    await expect(timeline.getByText('Parent conversation live frame')).toBeVisible();
    await page.getByTestId('emit-stale-frame').click();
    await page.getByTestId('emit-cross-scope-frame').click();
    await expect(timeline.getByText('stale frame')).toHaveCount(0);
    await expect(timeline.getByText('cross-scope frame')).toHaveCount(0);
  });

  test('keeps a newer public realtime frame when Refresh resolves an older snapshot', async ({ page }) => {
    const timeline = page.getByTestId('scoped-chat-v2-fixture').getByTestId('scoped-timeline-shell');
    await expect(timeline.locator('[data-cursor]')).toHaveAttribute('data-cursor', /fixture-conversation/);
    await page.getByTestId('hold-refresh-snapshot').click();
    await timeline.getByRole('button', { name: 'Refresh timeline' }).click();
    await page.getByTestId('emit-active-frame').click();
    await expect(timeline.getByText('Parent conversation live frame')).toBeVisible();
    const liveCursor = await timeline.locator('[data-cursor]').getAttribute('data-cursor');
    await page.getByTestId('resolve-refresh-snapshot').click();
    await expect(timeline.getByText('Parent conversation live frame')).toBeVisible();
    await expect(timeline.locator('[data-cursor]')).toHaveAttribute('data-cursor', liveCursor ?? '');
  });

  test('keeps only the final scope after rapid parent-child-grandchild switching', async ({ page }) => {
    const timeline = page.getByTestId('scoped-timeline-shell');
    await page.getByTestId('scope-parent').click();
    await page.getByTestId('scope-child').click();
    await page.getByTestId('scope-grandchild').click();
    await expect(page.getByTestId('active-scope')).toContainText('Grandchild');
    await page.waitForTimeout(100);
    await expect(timeline.locator('[data-scope-key="session:fixture-grandchild"]')).toBeVisible();
    await expect(timeline.getByText('Grandchild delegated session event 20', { exact: true })).toBeVisible();
    await expect(timeline.getByText('Parent conversation event 20', { exact: true })).toHaveCount(0);
    await expect(timeline.getByText('Child delegated session event 20', { exact: true })).toHaveCount(0);
  });

  test('keeps concurrent runtime subscriptions isolated and cleans up the navigated scope', async ({ page }) => {
    const subscriptions = () => page.evaluate(() => {
      const controller = (window as typeof window & {
        __scopedFixtureController?: { activeSubscriptions: string[] };
      }).__scopedFixtureController;
      return controller?.activeSubscriptions ?? [];
    });
    await expect.poll(subscriptions).toEqual(['conversation:fixture-conversation', 'session:fixture-child']);
    await page.getByTestId('scope-grandchild').click();
    await expect.poll(subscriptions).toEqual(['conversation:fixture-conversation', 'session:fixture-child', 'session:fixture-grandchild']);
    await page.getByTestId('back-parent').click();
    await expect.poll(subscriptions).toEqual(['conversation:fixture-conversation', 'session:fixture-child']);
    await expect.poll(() => page.evaluate(() => {
      const controller = (window as typeof window & {
        __scopedFixtureController?: { operationLog: string[] };
      }).__scopedFixtureController;
      return controller?.operationLog.some((entry) => entry === 'unsubscribe:session:fixture-grandchild') ?? false;
    })).toBe(true);
  });

  test('reconnects mounted parent and child timelines with independent public cursors', async ({ page }) => {
    const operations = () => page.evaluate(() => {
      const controller = (window as typeof window & {
        __scopedFixtureController?: { operationLog: string[] };
      }).__scopedFixtureController;
      return controller?.operationLog ?? [];
    });
    const parent = page.getByTestId('concurrent-parent');
    const child = page.getByTestId('concurrent-child');
    await expect(parent.locator('[data-cursor]')).toHaveAttribute('data-cursor', /conversation:fixture-conversation/);
    await expect(child.locator('[data-cursor]')).toHaveAttribute('data-cursor', /session:fixture-child/);

    const beforeReconnect = (await operations()).length;
    await page.getByTestId('reconnect').click();
    await expect.poll(async () => (await operations()).slice(beforeReconnect)).toEqual(expect.arrayContaining([
      expect.stringMatching(/^subscribe:conversation:fixture-conversation:fixture-conversation:fixture-conversation-cursor-/),
      expect.stringMatching(/^subscribe:session:fixture-child:fixture-session:fixture-child-cursor-/),
    ]));
    expect((await operations()).slice(beforeReconnect).some((entry) => entry.startsWith('reconnect:'))).toBe(false);

    await page.getByTestId('scope-parent').click();
    await page.getByTestId('emit-active-frame').click();
    await expect(parent.getByText('Parent conversation live frame')).toBeVisible();
    await page.getByTestId('scope-child').click();
    await page.getByTestId('emit-active-frame').click();
    await expect(child.getByText('Child delegated session live frame')).toBeVisible();
    await expect(parent.getByText('Child delegated session live frame')).toHaveCount(0);
    await expect(child.getByText('Parent conversation live frame')).toHaveCount(0);
  });
});
