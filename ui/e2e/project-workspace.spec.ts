import { expect, test, type Page } from '@playwright/test';
import { installTaskCockpitFixture, TASK_ID } from './task-cockpit-fixture';
import { installLibraryRoutes, libraryCatalog } from './library-fixture';
import type { PaletteItem } from '../src/lib/palette/model';

// Keep deterministic HTTP fixtures authoritative against production preview builds.
test.use({
  serviceWorkers: 'block',
  // Static preview lacks the controller's CSP hashes; these are mocked UI tests.
  bypassCSP: process.env.COGNIS_E2E_STATIC_PREVIEW === '1',
});

const browserErrors = new WeakMap<Page, string[]>();

for (const width of [390, 1440]) {
  test(`project waiting lane follows pending input and unread completion at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 900 });
    await projectFixture(page);
    let push: ((event: object) => void) | undefined;
    await page.routeWebSocket('**/api/ws', socket => {
      push = event => socket.send(JSON.stringify(event));
      push({ type: 'authenticated' });
      socket.onMessage(message => {
        if (JSON.parse(String(message)).type === 'ping') push?.({ type: 'pong' });
      });
    });
    let pending = true;
    let unread = false;
    let failWaitingOnce = width === 1440;
    const conversation = () => ({
      conversation_id: 'conv-task-chat', agent_id: 'agent-stage39', title: 'Kitchen renovation',
      context: { type: 'web' }, status: 'active', project_id: 'proj_home',
      has_active_turn: pending, has_unread: unread,
      pending_notification_types: pending ? ['step_question'] : [],
    });
    await page.route('**/api/v1/projects/proj_home/conversations/page**', route =>
      route.fulfill({ json: { items: [conversation()], cursor: null, has_more: false } }));
    await page.route('**/api/v1/conversations/waiting**', route => {
      expect(new URL(route.request().url()).searchParams.get('project_id')).toBe('proj_home');
      if (failWaitingOnce) {
        failWaitingOnce = false;
        return route.fulfill({ status: 503, json: { detail: 'Waiting temporarily unavailable' } });
      }
      return route.fulfill({ json: {
        conversations: { items: pending || unread ? [conversation()] : [], cursor: null, has_more: false },
        active_conversation_ids: pending ? ['conv-task-chat'] : [], activity_complete: true,
      } });
    });
    await page.goto('/projects/proj_home?tab=conversations');
    const waiting = page.getByTestId('dashboard-conversations-waiting');
    await expect(waiting.getByText('Kitchen renovation', { exact: true })).toBeVisible();
    await expect(waiting.getByText('Question waiting', { exact: true })).toBeVisible();
    await expect(page.getByTestId('dashboard-conversations-active').getByText('Kitchen renovation')).toHaveCount(0);
    await page.screenshot({ path: test.info().outputPath(`project-pending-${width}.png`) });
    pending = false;
    unread = true;
    push!({ type: 'scope_invalidated', reason: 'notification_state_changed' });
    await expect(waiting.getByText('Kitchen renovation', { exact: true })).toBeVisible();
    await expect(waiting.getByText('Question waiting', { exact: true })).toHaveCount(0);
    await page.screenshot({ path: test.info().outputPath(`project-finished-waiting-${width}.png`) });
    unread = false;
    push!({ type: 'scope_invalidated', reason: 'sidebar_changed' });
    await expect(waiting.getByText('Kitchen renovation', { exact: true })).toHaveCount(0);
    await expect(page.getByTestId('dashboard-conversations-recent').getByText('Kitchen renovation', { exact: true })).toBeVisible();
  });
}

test('project list reactivates the same minimized conversation and reconciles answered input on focus', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  const fixture = await projectFixture(page, true);
  await page.route('**/api/v1/feedback/conversations/conv-task-chat/staging', route => route.fulfill({ json: {
    conversation_id: 'conv-task-chat', version: 1, items: [], receipts: [], subject_ids: [],
  } }));
  let pending = true;
  await page.route('**/api/v1/notifications**', route => route.fulfill({ json: pending ? [{
    notification_id: 'question-project', conversation_id: 'conv-task-chat',
    notification_type: 'step_question', status: 'pending', payload: {
      question: 'Choose a rollout mode.', questions: [{
        id: 'rollout', question: 'Choose a rollout mode.',
        options: [{ id: 'canary', label: 'Canary' }], allow_custom: false, required: true,
      }],
    },
  }] : [] }));
  await page.goto('/projects/proj_home?tab=conversations');
  const row = page.getByRole('button', { name: /Kitchen renovation/ }).first();
  await row.click();
  const chat = page.getByTestId('workspace-window-conversation:conv-task-chat');
  await expect(chat).toHaveAttribute('data-minimized', 'false');
  await expect(chat.getByRole('button', { name: 'Send response' })).toBeVisible();
  const selectedUrl = page.url();
  await chat.getByRole('button', { name: 'Minimize', exact: true }).click();
  await expect(chat).toHaveAttribute('data-minimized', 'true');
  pending = false;
  await page.evaluate(() => window.dispatchEvent(new Event('focus')));
  await row.click();
  await expect(chat).toHaveAttribute('data-minimized', 'false');
  await expect(chat.getByRole('button', { name: 'Send response' })).toHaveCount(0);
  expect(page.url()).toBe(selectedUrl);
  await expect(page.getByTestId('workspace-window-conversation:conv-task-chat')).toHaveCount(1);
  expect(fixture.unmockedRequests()).toEqual([]);
  await page.screenshot({ path: test.info().outputPath('project-reactivated-chat.png') });
});

for (const width of [390, 1440]) {
test(`project lists refresh on pushes and child work remains in Active now at ${width}px`, async ({ page }) => {
  await page.setViewportSize({ width, height: 900 });
  await projectFixture(page);
  let push: ((event: object) => void) | undefined;
  await page.routeWebSocket('**/api/ws', (socket) => {
    push = (event) => socket.send(JSON.stringify(event));
    push({ type: 'authenticated' });
    socket.onMessage((message) => {
      if (JSON.parse(String(message)).type === 'ping') push?.({ type: 'pong' });
    });
  });
  let title = 'Child work owner';
  let running = true;
  let conversationReads = 0;
  let taskReads = 0;
  let activityReads = 0;
  let scheduleReads = 0;
  await page.route('**/api/v1/projects/proj_home/conversations/page**', (route) => {
    conversationReads++;
    return route.fulfill({ json: { items: [{
      conversation_id: 'conv-task-chat', agent_id: 'agent-stage39', title,
      context: { type: 'web' }, status: 'active', project_id: 'proj_home',
      has_active_turn: false, has_unread: true, pending_notification_types: [],
    }], cursor: null, has_more: false } });
  });
  await page.route('**/api/v1/projects/proj_home/tasks/page**', (route) => {
    taskReads++;
    return route.fulfill({ json: { items: [], cursor: null, has_more: false } });
  });
  await page.route('**/api/v1/projects/proj_home/activity**', (route) => {
    activityReads++;
    return route.fulfill({ json: { deliverables: [], artifacts: [] } });
  });
  await page.route('**/api/v1/schedules?**', (route) => {
    scheduleReads++;
    return route.fulfill({ json: [] });
  });
  await page.route('**/api/v1/conversations/background-work**', (route) => route.fulfill({ json: {
    background_work_changed: true, agent_direct_chats: [],
    background_work: { items: running ? [{
      kind: 'managed_conversation', status: 'running',
      controller_conversation_id: 'conv-task-chat', work_id: 'child-work',
    }] : [], active_count: running ? 1 : 0, truncated: false },
  } }));
  await page.goto('/projects/proj_home?tab=conversations');
  const active = page.getByTestId('dashboard-conversations-active');
  await expect(active.getByText(title, { exact: true })).toBeVisible();
  await expect(active.getByLabel('Background work active', { exact: true })).toBeVisible();
  await page.screenshot({ path: test.info().outputPath('project-child-work-active.png') });
  await expect.poll(() => Boolean(push)).toBe(true);
  const before = [conversationReads, taskReads, activityReads, scheduleReads];
  title = 'Updated live without navigation';
  push!({ type: 'scope_invalidated', reason: 'task_progress_changed' });
  await expect(active.getByText(title, { exact: true })).toBeVisible();
  await expect.poll(() => [conversationReads, taskReads, activityReads, scheduleReads]
    .every((count, index) => count > before[index])).toBe(true);
  running = false;
  push!({ type: 'work_invalidated', revision: 2 });
  await expect(page.getByTestId('dashboard-conversations-recent').getByText(title, { exact: true })).toBeVisible();
  await expect(active.getByText(title, { exact: true })).toHaveCount(0);
  await page.screenshot({ path: test.info().outputPath('project-live-state.png') });
});
}

test('project pop-out opens the selected tab beside New chat', async ({ page }) => {
  await projectFixture(page);
  await page.goto('/projects/proj_home?tab=tasks');
  const popout = page.getByRole('button', { name: 'Open project in separate window', exact: true });
  const newChat = page.getByRole('button', { name: 'New chat in project', exact: true });
  await expect(popout).toBeVisible();
  const left = await popout.boundingBox();
  const right = await newChat.boundingBox();
  expect(left!.x + left!.width).toBeLessThanOrEqual(right!.x);
  await page.evaluate(() => {
    window.open = (...args: Parameters<typeof window.open>) => {
      sessionStorage.setItem('project-popup', JSON.stringify(args));
      return null;
    };
  });
  await popout.click();
  const [href, name, features] = await page.evaluate(() => JSON.parse(sessionStorage.getItem('project-popup')!));
  const url = new URL(href);
  expect(url.pathname).toBe('/projects/proj_home');
  expect(url.searchParams.get('tab')).toBe('tasks');
  expect(url.searchParams.get('window')).toBe('1');
  expect(name).toBe('cognis-project-proj_home');
  expect(features).toContain('noopener');
  await expect(page.getByText('This device does not support separate project windows.')).toHaveCount(0);
});

test('window flag does not hide navigation on the project list', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  await projectFixture(page);
  await page.goto('/projects?view=all&window=1');
  await expect(page.getByRole('heading', { name: 'Projects', exact: true })).toBeVisible();
  await expect(page.locator('aside')).toBeVisible();
});

for (const width of [1440, 390]) {
  test(`project window stays focused and omits missing description at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 900 });
    await projectFixture(page, false, false, null);
    await page.goto('/projects/proj_home?window=1');
    await expect(page.getByRole('heading', { name: 'Home', exact: true })).toBeVisible();
    await expect(page.getByRole('link', { name: '← All projects', exact: true })).toHaveCount(0);
    await expect(page.getByRole('button', { name: 'Open project in separate window', exact: true })).toHaveCount(0);
    await expect(page.getByText('No description', { exact: true })).toHaveCount(0);
    await expect(page.getByRole('button', { name: 'Open navigation', exact: true })).toHaveCount(0);
    await expect(page.locator('aside')).toHaveCount(0);
    await page.getByRole('tab', { name: /^Conversations\b/ }).click();
    await expect(page).toHaveURL(url => url.searchParams.get('window') === '1' && url.searchParams.get('tab') === 'conversations');
    await expect(page.getByRole('button', { name: 'New chat in project', exact: true })).toBeVisible();
  });
}

test('Projects resumes the last project tab and supports explicit list and revoked access', async ({ page }) => {
  await projectFixture(page);
  await page.goto('/projects/proj_home?tab=settings');
  await expect.poll(() => page.evaluate(() => localStorage.getItem('cognis.projects.lastOpened:owner@cognis.test'))).toContain('"tab":"settings"');
  await page.goto('/projects');
  await expect(page).toHaveURL(/\/projects\/proj_home\?tab=settings$/);
  await page.getByRole('link', { name: '← All projects', exact: true }).click();
  await expect(page).toHaveURL(/\/projects\?view=all$/);
  await expect(page.getByRole('heading', { name: 'Projects', exact: true })).toBeVisible();
  await page.getByRole('link', { name: 'Open Projects', exact: true }).first().click();
  await expect(page).toHaveURL(/\/projects\/proj_home\?tab=settings$/);
  await page.route('**/api/v1/projects', (route) => route.fulfill({ json: [] }));
  await page.goto('/projects');
  await expect(page.getByText('No projects yet.', { exact: false })).toBeVisible();
  await expect(page).toHaveURL(/\/projects$/);
  expect(await page.evaluate(() => localStorage.getItem('cognis.projects.lastOpened:owner@cognis.test'))).toBeNull();
});
test('project knowledge assignment and shared reader stay within project', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await projectFixture(page);
  const kb = { knowledgebase_id: 'kb_home', name: 'House plans', description: 'Shared plans',
    owner_email: 'owner@cognis.test', access_level: 'owner', status: 'active',
    settings: {}, metadata_schema: {}, source_count: 0, chunk_count: 0 };
  let assigned = false;
  await page.route('**/api/v1/projects/proj_home/knowledgebases**', (route) => {
    if (route.request().method() === 'PUT') assigned = true;
    if (route.request().method() === 'DELETE') assigned = false;
    return route.fulfill({ json: route.request().method() === 'GET' ? (assigned ? [kb] : []) : { assigned } });
  });
  await page.route('**/api/v1/knowledgebases/**', (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path.endsWith('/capabilities')) return route.fulfill({ json: { enabled: true, notes: [], retrieval_available: true } });
    if (path.endsWith('/documents')) return route.fulfill({ json: { documents: [], cursor: null, has_more: false } });
    return route.fulfill({ json: path.endsWith('/stats') ? { knowledgebase_id: kb.knowledgebase_id, sources_by_status: {}, chunk_count: 0 }
      : path.endsWith('/sources') ? [] : path.endsWith('/kb_home') ? { ...kb, access_level: 'shared' } : [kb] });
  });
  await page.goto('/projects/proj_home?tab=knowledge');
  await page.getByRole('combobox', { name: 'Knowledgebase to share' }).selectOption('kb_home');
  await page.getByRole('button', { name: 'Share with project' }).click();
  await expect(page.getByText('House plans', { exact: true })).toBeVisible();
  await page.getByRole('button', { name: 'Open', exact: true }).click();
  await expect(page.getByRole('tab', { name: 'Search', exact: true })).toBeVisible();
  await expect(page.getByRole('dialog', { name: 'Knowledgebase' }).getByRole('tab', { name: 'Settings', exact: true })).toHaveCount(0);
  expect(new URL(page.url()).pathname).toBe('/projects/proj_home');
  await page.getByRole('dialog', { name: 'Knowledgebase' }).getByRole('button', { name: 'Knowledge', exact: true }).click();
  await page.getByRole('button', { name: 'Remove from project' }).click();
  await expect(page.getByText('No knowledgebases shared with this project.')).toBeVisible();
});
test.afterEach(({ page }) => {
  expect(browserErrors.get(page) ?? []).toEqual([]);
});
// Frontend integration against deterministic API fixtures, not a live controller.
async function projectFixture(page: Page, windows = false, reader = false, description: string | null = 'Household and reconstruction') {
  const errors: string[] = [];
  browserErrors.set(page, errors);
  page.on('pageerror', (error) => errors.push(error.message));
  const cockpit = await installTaskCockpitFixture(page, { dashboardWorkspaceWindows: windows });
  await page.route('**/api/v1/inbox**', route => route.fulfill({ json: {
    items: [], unread_count: 0, next_cursor: null,
  } }));
  await page.route('**/api/v1/dashboard/navigation-summary', route => route.fulfill({ json: {
    chat_groups: [], dashboard: { total: 0, critical: 0, warning: 0, info: 0, truncated: false },
    schedules: 0, tasks: 0,
  } }));
  await page.route('**/api/v1/conversations/waiting**', route => route.fulfill({ json: {
    conversations: { items: [], cursor: null, has_more: false }, active_conversation_ids: [], activity_complete: true,
  } }));
  await installLibraryRoutes(page, [
    { ...libraryCatalog[0], id: 'dlv_project_report', lineage_id: 'dlv_project_report', title: 'Renovation plan', project_id: 'proj_home', project_name: 'Home', conversation_id: 'conv-task-chat', conversation_title: 'Kitchen renovation' },
    { ...libraryCatalog[7], id: 'art_floor_plan', title: 'floor-plan.png', project_id: 'proj_home', project_name: 'Home', format: 'image/png' },
  ]);
  await page.route('**/api/v1/library/deliverables/dlv_project_report/content', (route) => route.fulfill({ json: {
    deliverable_id: 'dlv_project_report', title: 'Renovation plan', format: 'markdown', content: '# Kitchen plan\n\nKeep the original oak floor.', status: 'delivered',
  } }));
  await page.routeWebSocket('**/api/ws', (socket) => {
    socket.send(JSON.stringify({ type: 'authenticated' }));
    socket.onMessage((message) => {
      if (JSON.parse(String(message)).type === 'ping') socket.send(JSON.stringify({ type: 'pong' }));
    });
  });
  await page.route('**/api/auth/me', (route) => route.fulfill({ json: { email: 'owner@cognis.test', role: 'user', name: 'Owner' } }));
  await page.route('**/api/health**', (route) => route.fulfill({ json: { status: 'ok', providers: {} } }));
  const project = {
    project_id: 'proj_home', name: 'Home', description,
    status: 'active', instructions: '', sources: [], workflow_ids: [],
    agents: [{ agent_id: 'agent-stage39', project_id: 'proj_home', agent_name: 'Butler', enabled: true }],
    grants: [], caller_role: reader ? 'member' : 'owner', active_schedule_count: 1,
    is_readonly_for_caller: reader, is_shared_with_me: reader,
  };
  const conversation = {
    conversation_id: 'conv-task-chat', agent_id: 'agent-stage39', title: 'Kitchen renovation',
    project_id: project.project_id, status: 'active', context: { type: 'web' },
    user_email: reader ? 'partner@cognis.test' : 'owner@cognis.test',
    visibility: 'project', is_readonly_for_caller: reader, can_continue: reader, active_session_id: null,
  };
  await page.route('**/api/v1/skills', (route) => route.fulfill({ json: [] }));
  await page.route('**/api/v1/projects', (route) => route.fulfill({ json: [project] }));
  let created: Record<string, unknown> | null = null;
  await page.route('**/api/v1/agents?**', (route) => route.fulfill({ json: {
    items: [{ agent_id: 'agent-stage39', name: 'Butler', display_name: 'Butler', status: 'active', agent_type: 'primary', profiles: [] }],
    has_more: false, next_cursor: null,
  } }));
  await page.route('**/api/v1/projects/**', (route) => {
    const path = new URL(route.request().url()).pathname;
    const json = path.endsWith('/conversations/page') ? { items: [conversation], cursor: null, has_more: false }
      : path.endsWith('/tasks/page') ? { items: [{ task_id: TASK_ID, title: 'Release safety review', status: 'completed', agent_id: 'agent-stage39', created_by: 'owner@cognis.test', updated_at: '2026-10-04T05:00:00Z' }], cursor: null, has_more: false }
      : path.endsWith('/agents') ? []
      : path.endsWith('/activity') ? {
        deliverables: [{ deliverable_id: 'dlv_project_report', title: 'Renovation plan', format: 'markdown', conversation_id: conversation.conversation_id, content_preview: 'Keep the original oak floor.', step_run_id: 'step_project_report', is_readonly_for_caller: reader }],
        artifacts: [{ artifact_id: 'art_floor_plan', filename: 'floor-plan.png', mime_type: 'image/png', size_bytes: 80, kind: 'file' }]
      } : project;
    return route.fulfill({ json });
  });
  await page.route('**/api/v1/schedules**', (route) => {
    const schedule = { schedule_id: 'sch_home', name: 'Household weekly review', project_id: project.project_id,
      enabled: true, agent_id: 'agent-stage39', task_template: {}, schedule_type: 'cron', workflow_id: null, timezone: 'UTC', cron_expr: '0 9 * * 1', human_schedule: 'Every Monday at 9:00 AM', consecutive_errors: 0, max_concurrent_runs: 1, completion_mode_family: 'default', allow_silent_completion: false, retry_failed_tasks: false, fail_paused_task_on_next_fire: true };
    const path = new URL(route.request().url()).pathname;
    return route.fulfill({ json: path.endsWith('/runs') ? [] : path === '/api/v1/schedules' ? [schedule] : schedule });
  });
  await page.route('**/api/v1/conversations/conv-task-chat?**', (route) => route.fulfill({ json: conversation }));
  await page.route('**/api/v1/conversations', (route) => {
    if (route.request().method() !== 'POST') return route.fallback();
    created = route.request().postDataJSON();
    return route.fulfill({ json: conversation });
  });
  await page.route('**/api/v1/deliverables/dlv_project_report', (route) => route.fulfill({ json: {
    deliverable_id: 'dlv_project_report', title: 'Renovation plan', format: 'markdown', content: '# Kitchen plan\n\nKeep the original oak floor.', status: 'published',
  } }));
  await page.route('**/api/v1/step-runs/step_project_report/deliverables/dlv_project_report', (route) => route.fulfill({ json: {
    deliverable_id: 'dlv_project_report', title: 'Renovation plan', format: 'markdown', content: '# Kitchen plan\n\nKeep the original oak floor.', status: 'published',
  } }));
  await page.route('**/api/v1/artifacts/art_floor_plan/signed-url**', (route) => route.fulfill({ json: {
    url: 'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jJ9kAAAAASUVORK5CYII='
  } }));
  return { created: () => created, unmockedRequests: cockpit.unmockedRequests };
}

const scopedResults = [
  { kind: 'conversation', id: 'conv-task-chat' },
  { kind: 'task', id: TASK_ID },
  { kind: 'schedule', id: 'sch_home' },
  { kind: 'deliverable', id: 'dlv_release_review' },
  { kind: 'artifact', id: 'att_requirements' },
] as const;

async function projectSpotlight(page: Page, kind: PaletteItem['kind'], id: string, project_id: string | null = 'proj_home') {
  const item: PaletteItem = {
    kind, id, project_id, title: `Scoped ${kind}`, status: 'active',
    agent_id: 'agent-stage39', agent_name: 'Butler', format: null, updated_at: '2026-10-04T05:00:00Z',
    href: kind === 'artifact' || kind === 'deliverable' ? `/library/${kind}/${id}`
      : `/${kind === 'conversation' ? 'chat' : kind === 'task' ? 'tasks' : 'schedules'}/${id}`,
  };
  await page.route('**/api/v1/search/palette?**', route => route.fulfill({ json: {
    items: [item], next_cursor: null,
  } }));
  await expect(page.getByRole('tab', { name: 'Overview', exact: true })).toBeVisible();
  await page.keyboard.press('Meta+k');
  const input = page.getByRole('combobox', { name: 'Search Cognis' });
  await expect(input).toBeFocused();
  await input.fill(item.title);
  await expect(page.getByRole('dialog', { name: 'Command palette', exact: true }).getByRole('option')).toHaveCount(1);
  return input;
}

for (const windows of [false, true]) {
  for (const { kind, id } of scopedResults) {
    test(`Spotlight opens project ${kind} from Settings with windows ${windows ? 'enabled' : 'disabled'}`, async ({ page }) => {
      await projectFixture(page, windows);
      // These accessible documents/files deliberately are not in recent project activity.
      await installLibraryRoutes(page, libraryCatalog.map(row => ({ ...row, project_id: 'proj_home', project_name: 'Home' })));
      await page.goto('/projects/proj_home?tab=settings');
      await expect(page.getByRole('tab', { name: 'Settings', exact: true })).toHaveAttribute('aria-selected', 'true');
      const input = await projectSpotlight(page, kind, id);
      await input.press('Enter');
      await expect(page).toHaveURL(url => url.pathname === '/projects/proj_home'
        && url.searchParams.get('tab') === 'settings'
        && url.searchParams.get('view') === kind && url.searchParams.get('item') === id);
      await expect(input).toBeHidden();
      if (windows) await expect(page.getByTestId(`workspace-window-${kind}:${id}`)).toBeVisible();
      else await expect(page.getByRole('dialog')).toBeVisible();
      if (kind === 'deliverable' || kind === 'artifact') {
        await expect(page.getByTestId('resource-reader')).toBeVisible();
        await expect(page.getByTestId('resource-reader')).toContainText(kind === 'deliverable'
          ? 'The October release is ready for final acceptance.' : 'Keep finished work easy to find.');
        await expect(page.getByText('Resource unavailable', { exact: true })).toBeHidden();
      }
    });
  }
}

for (const scope of ['proj_other', null]) {
  test(`Spotlight redirects ${scope ? 'foreign-project' : 'unscoped'} results from an open project`, async ({ page }) => {
    await projectFixture(page, true);
    await page.goto('/projects/proj_home?tab=settings');
    const input = await projectSpotlight(page, 'task', TASK_ID, scope);
    await input.press('Enter');
    await expect(page).toHaveURL(url => url.pathname === `/tasks/${TASK_ID}`);
  });
}

test('Spotlight respects explicit redirects and phone project modal fallback', async ({ page }) => {
  await projectFixture(page, true);
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto('/projects/proj_home?tab=overview');
  let input = await projectSpotlight(page, 'conversation', 'conv-task-chat');
  await input.press('Enter');
  await expect(page).toHaveURL(url => url.pathname === '/projects/proj_home' && url.searchParams.get('view') === 'conversation');
  await expect(input).toBeHidden();
  await expect(page.getByRole('dialog')).toBeVisible();
  await expect(page.getByTestId('workspace-window-conversation:conv-task-chat')).toBeHidden();
  await page.keyboard.press('Escape');
  await expect(page.getByRole('dialog')).toBeHidden();
  input = await projectSpotlight(page, 'task', TASK_ID);
  await input.press('Meta+Enter');
  await expect(page).toHaveURL(url => url.pathname === `/tasks/${TASK_ID}`);
});

test('project icon picker saves theme-aware identity and can switch back to image', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await projectFixture(page);
  await page.goto('/projects/proj_home?tab=settings');
  let model = await page.evaluate(async () => (await fetch('/api/v1/projects/proj_home')).json());
  await page.route('**/api/v1/projects/proj_home', (route) => {
    if (route.request().method() === 'PUT' || route.request().method() === 'PATCH') {
      model = { ...model, ...route.request().postDataJSON() };
    }
    return route.fulfill({ json: model });
  });
  await expect(page.getByRole('button', { name: 'Save project', exact: true })).toBeEnabled();
  await page.getByRole('button', { name: 'Use Travel icon', exact: true }).click();
  await expect(page.getByRole('button', { name: 'Use Travel icon', exact: true })).toHaveAttribute('aria-pressed', 'true');
  await page.getByRole('button', { name: 'Save project', exact: true }).click();
  await expect.poll(() => model.metadata?.icon).toBe('suitcase');
  await expect(page.getByRole('button', { name: 'View project avatar' }).getByTestId('project-library-icon')).toBeVisible();
  await page.getByRole('group', { name: 'Project icon' }).scrollIntoViewIfNeeded();
  await page.screenshot({ path: test.info().outputPath('project-icon-picker.png') });
  model.avatar_url = 'data:image/svg+xml,' + encodeURIComponent('<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16"><rect width="16" height="16" fill="red"/></svg>');
  await page.reload();
  await expect(page.getByRole('button', { name: 'View project avatar' }).locator('img')).toBeVisible();
  await page.getByRole('button', { name: 'Remove avatar', exact: true }).click();
  await expect.poll(() => model.avatar_url).toBeNull();
  expect(model.metadata.icon).toBe('suitcase');
  await expect(page.getByRole('button', { name: 'View project avatar' }).getByTestId('project-library-icon')).toBeVisible();
  await page.getByRole('button', { name: /Default/ }).click();
  await page.getByRole('button', { name: 'Save project', exact: true }).click();
  await expect.poll(() => model.metadata.icon).toBeNull();
});

test('SVG project avatars upload only inert PNG pixels and reject malformed SVG', async ({ page }) => {
  await projectFixture(page);
  let uploaded: Buffer | null = null;
  let uploadedCount = 0;
  await page.route('**/api/v1/images/upload', (route) => {
    uploaded = route.request().postDataBuffer();
    uploadedCount++;
    return route.fulfill({ json: { image_id: 'img_project_svg', url: '/api/v1/images/img_project_svg' } });
  });
  await page.goto('/projects/proj_home?tab=settings');
  const fileInput = page.locator('input[type=file]');
  await fileInput.setInputFiles({
    name: 'logo.svg', mimeType: 'image/svg+xml',
    buffer: Buffer.from('<svg xmlns="http://www.w3.org/2000/svg" width="64" height="32"><script>window.svgExecuted=true</script><rect width="64" height="32" fill="#38bdf8"/></svg>'),
  });
  await expect.poll(() => uploadedCount).toBe(1);
  expect(uploaded!.includes(Buffer.from('image/png'))).toBe(true);
  expect(uploaded!.includes(Buffer.from([137, 80, 78, 71, 13, 10, 26, 10]))).toBe(true);
  expect(uploaded!.includes(Buffer.from('<svg'))).toBe(false);
  expect(await page.evaluate(() => (window as unknown as { svgExecuted?: boolean }).svgExecuted)).toBeUndefined();
  await expect(page.getByText('Project avatar uploaded', { exact: true })).toBeVisible();
  await fileInput.setInputFiles({ name: 'broken.svg', mimeType: 'image/svg+xml', buffer: Buffer.from('<svg>broken') });
  await expect(page.getByText('Invalid SVG avatar', { exact: true })).toBeVisible();
  expect(uploadedCount).toBe(1);
});

for (const width of [1440, 390]) {
  test(`project is home for chats, tasks, schedules and assets at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 900 });
    await projectFixture(page);
    await page.goto('/projects/proj_home');
    await expect(page.getByRole('heading', { name: 'Home', exact: true })).toBeVisible({ timeout: 30_000 });
    await page.screenshot({ path: test.info().outputPath(`project-home-${width}.png`) });
    await page.getByRole('button', { name: /Kitchen renovation/ }).first().click();
    await expect(page.getByTestId('dashboard-entity-modal')).toBeVisible();
    expect(new URL(page.url()).pathname).toBe('/projects/proj_home');
    await page.goBack();
    await expect(page.getByTestId('dashboard-entity-modal')).toHaveCount(0);
    await page.getByRole('button', { name: /Release safety review/ }).first().click();
    await expect(page.getByTestId('dashboard-task-modal-tab-description')).toBeVisible();
    await page.screenshot({ path: test.info().outputPath(`project-task-${width}.png`) });
    await page.getByTestId('dashboard-entity-modal-close').click();
    await page.getByTestId('project-tab-schedules').click();
    await page.getByRole('button', { name: /Household weekly review/ }).click();
    await expect(page.getByTestId('dashboard-schedule-modal')).toBeVisible();
    await expect.poll(() => page.getByTestId('dashboard-schedule-modal-scroll-shell').evaluate((node) => Math.max(...[node, ...node.querySelectorAll('section')].map((item) => item.scrollWidth - item.clientWidth)))).toBeLessThanOrEqual(1);
    await page.getByTestId('dashboard-schedule-modal-close').click();
    await page.getByTestId('project-tab-resources').click();
    await expect(page.getByRole('link', { name: /Renovation plan/ })).toBeVisible();
    await page.screenshot({ path: test.info().outputPath(`project-resources-library-${width}.png`), fullPage: true });
    await page.getByRole('link', { name: /Renovation plan/ }).click();
    await expect(page.locator('p').getByText('Keep the original oak floor.', { exact: true })).toBeVisible();
    if (width === 390) await page.getByRole('dialog').getByRole('button', { name: 'Show details', exact: true }).click();
    await expect(page.getByRole('heading', { name: 'Details', exact: true })).toBeVisible();
    if (width === 390) await page.getByRole('dialog').getByRole('button', { name: 'Hide details', exact: true }).click();
    await page.screenshot({ path: test.info().outputPath(`project-asset-${width}.png`) });
    await page.goBack();
    await page.getByRole('tab', { name: 'Files', exact: true }).click();
    await page.getByRole('link', { name: /floor-plan.png/ }).click();
    await expect(page.getByRole('img', { name: 'floor-plan.png' })).toBeVisible();
    expect(new URL(page.url()).pathname).toBe('/projects/proj_home');
    await expect(page.getByTestId('library-resource-modal')).toBeVisible();
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(width);
  });
}

for (const width of [320, 390, 430]) {
  test(`project overview contains long content at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 900 });
    await projectFixture(page);
    await page.route('**/api/v1/projects/proj_home/conversations**', (route) => route.fulfill({ json: { items: [{
      conversation_id: 'conv-task-chat', agent_id: 'agent-stage39',
      title: 'Task: Weekly Cognis release / Step: implementation and validation '.repeat(5),
      user_email: 'collaborator-with-a-long-address@cognis.test', visibility: 'project',
      context: { type: 'web' },
    }], cursor: null, has_more: false } }));
    await page.route('**/api/v1/projects/proj_home/activity**', (route) => route.fulfill({ json: {
      deliverables: [{ deliverable_id: 'dlv_long', title: 'Weekly Cognis release check — blocked before release '.repeat(8), format: 'markdown', created_at: '2026-10-04T05:00:00Z' }],
      artifacts: [{ artifact_id: 'art_long', filename: 'very-long-file-name-'.repeat(30) + '.png', mime_type: 'image/png', owner_email: 'long-address-'.repeat(30) + '@cognis.test' }],
    } }));
    await page.goto('/projects/proj_home');
    await expect(page.getByRole('heading', { name: 'Conversations', exact: true })).toBeVisible();
    const panel = page.locator('#project-panel-overview');
    // The app shell can clip overflow; inspect the panel itself, not just the document.
    await expect.poll(() => panel.evaluate((node) => node.scrollWidth - node.clientWidth)).toBeLessThanOrEqual(1);
    for (const heading of ['Conversations', 'At a glance', 'Tasks', 'Recent deliverables & artifacts']) {
      const card = page.getByRole('heading', { name: heading, exact: true }).locator('..').filter({ has: page.locator('h2') });
      const bounds = await card.boundingBox();
      expect(bounds!.x + bounds!.width).toBeLessThanOrEqual(width);
    }
    await page.screenshot({ path: test.info().outputPath(`project-long-content-${width}.png`) });
    for (const tab of ['conversations', 'tasks', 'schedules', 'agents', 'resources', 'settings', 'sharing']) {
      await page.getByTestId(`project-tab-${tab}`).click();
      const tabPanel = page.locator(`#project-panel-${tab}`);
      await expect(tabPanel).toBeVisible();
      await expect.poll(() => tabPanel.evaluate((node) => node.scrollWidth - node.clientWidth), `overflow in ${tab}`).toBeLessThanOrEqual(1);
      if (tab === 'resources') await page.screenshot({ path: test.info().outputPath(`project-resources-${width}.png`) });
    }
  });
}

test('shared chat is read-only inside project', async ({ page }) => {
  const fixture = await projectFixture(page, false, true);
  await page.goto('/projects/proj_home?view=conversation&item=conv-task-chat');
  await expect(page.getByTestId('shared-modal-readonly')).toBeVisible();
  await expect(page.locator('[data-testid="dashboard-entity-modal"] textarea')).toHaveCount(0);
  await page.screenshot({ path: test.info().outputPath('project-reader.png') });
  await page.getByRole('button', { name: 'Continue in my chat' }).click();
  await expect.poll(() => fixture.created()?.project_id).toBe('proj_home');
  expect(new URL(page.url()).pathname).toBe('/projects/proj_home');
});

for (const resource of ['conversations', 'tasks']) {
 for (const width of [390, 1280]) {
  test(`project ${resource} use bounded dashboard cards and scroll through server pages at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 844 });
    await projectFixture(page);
    const requested: number[] = [];
    await page.route(`**/api/v1/projects/proj_home/${resource}/page**`, (route) => {
      const start = Number(new URL(route.request().url()).searchParams.get('cursor') ?? 0);
      requested.push(start);
      const items = Array.from({ length: 30 }, (_, i) => ({
        conversation_id: `conv_page_${start + i}`, task_id: `task_page_${start + i}`,
        title: `Project work ${start + i}`, status: resource === 'tasks' ? 'completed' : 'active',
        context: { type: 'web' }, agent_id: 'agent-stage39', user_email: 'owner@cognis.test',
        updated_at: '2026-10-04T05:00:00Z', last_message_at: '2026-10-04T05:00:00Z',
      }));
      return route.fulfill({ json: { items, cursor: start === 0 ? '30' : null, has_more: start === 0 } });
    });
    await page.goto(`/projects/proj_home?tab=${resource}`);
    const section = page.getByTestId(`dashboard-${resource}-section`);
    const list = page.getByTestId(`dashboard-${resource}-recent-list`);
    await expect(list).toBeVisible();
    await expect.poll(() => list.evaluate((node) => node.scrollHeight > node.clientHeight)).toBe(true);
    const initialHeight = await section.evaluate((node) => node.getBoundingClientRect().height);
    expect(initialHeight).toBeLessThan(850);
    const prefix = resource === 'tasks' ? 'task' : 'conversation';
    await expect(page.getByTestId(`dashboard-${prefix}-row-${prefix === 'task' ? 'task' : 'conv'}_page_0`)).toBeVisible();
    await expect.poll(async () => {
      await list.evaluate((node) => { node.scrollTop = node.scrollHeight; });
      await page.getByTestId(`dashboard-${resource}-recent-sentinel`).evaluateAll((nodes) => nodes.forEach((node) => node.scrollIntoView()));
      return requested.includes(30);
    }).toBe(true);
    const last = page.getByTestId(`dashboard-${prefix}-row-${prefix === 'task' ? 'task' : 'conv'}_page_59`);
    await expect(last).toBeVisible();
    await last.scrollIntoViewIfNeeded();
    await expect(last).toBeInViewport();
    expect(await section.evaluate((node) => node.getBoundingClientRect().height)).toBeCloseTo(initialHeight, 0);
    await page.screenshot({ path: test.info().outputPath(`project-${resource}-bounded-${width}.png`) });
    expect(requested).toContain(30);
    expect(new URL(page.url()).pathname).toBe('/projects/proj_home');
  });
 }
}

test('project uses dashboard agent strip and excludes task rows', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await projectFixture(page);
  await page.route('**/api/v1/projects/proj_home/agents', (route) => route.fulfill({ json: [
    { agent_id: 'agent-stage39', project_id: 'proj_home', name: 'Butler', enabled: true },
  ] }));
  await page.route('**/api/v1/projects/proj_home/conversations/page**', (route) => route.fulfill({ json: {
    items: [
      { conversation_id: 'conv-topic', agent_id: 'agent-stage39', title: 'Kitchen renovation', context: { type: 'web' }, status: 'active' },
      { conversation_id: 'conv-task', agent_id: 'agent-stage39', title: 'Task: Kitchen job', context: { type: 'task' }, status: 'active' },
    ], cursor: null, has_more: false,
  } }));
  let scope: string | null = null;
  await page.route('**/api/v1/conversations/resolve', (route) => {
    scope = route.request().postDataJSON().scope;
    return route.fulfill({ json: { conversation: {
      conversation_id: 'conv-task-chat', agent_id: 'agent-stage39', context: { type: 'web' },
      title: 'Butler', status: 'active',
    } } });
  });
  await page.goto('/projects/proj_home?tab=conversations');
  await expect(page.getByText('Task: Kitchen job', { exact: true })).toHaveCount(0);
  const strip = page.getByTestId('dashboard-conversations-agents');
  await expect(strip.getByRole('button', { name: /Butler/ })).toHaveCount(1);
  await strip.getByRole('button', { name: /Butler/ }).click();
  await expect.poll(() => scope).toBe('agent_direct');
  await expect(page.getByTestId('agent-quick-chat-modal')).toBeVisible();
  expect(new URL(page.url()).pathname).toBe('/projects/proj_home');
});

test('new chat always keeps the current project', async ({ page }) => {
  const fixture = await projectFixture(page);
  await page.goto('/projects/proj_home');
  await page.getByRole('button', { name: 'New chat in project' }).click();
  await page.getByRole('button', { name: 'Create conversation', exact: true }).click();
  await expect.poll(() => fixture.created()?.project_id).toBe('proj_home');
  await expect(page.getByTestId('dashboard-entity-modal')).toBeVisible();
  expect(new URL(page.url()).pathname).toBe('/projects/proj_home');
});

test('new chat reuses the Chat app agent selection instead of the first agent', async ({ page }) => {
  const fixture = await projectFixture(page);
  await page.route('**/api/v1/agents?**', (route) => route.fulfill({ json: {
    items: [
      { agent_id: 'agent-first', name: 'First', status: 'active', agent_type: 'primary', profiles: [] },
      { agent_id: 'agent-stage39', name: 'Butler', status: 'active', agent_type: 'primary', profiles: [] },
    ], has_more: false, next_cursor: null,
  } }));
  await page.addInitScript(() => localStorage.setItem('cognis-chat-selected-agent', JSON.stringify(['agent-stage39'])));
  await page.goto('/projects/proj_home');
  await page.getByRole('button', { name: 'New chat in project' }).click();
  await expect(page.getByRole('combobox', { name: 'Primary agent', exact: true })).toHaveValue('agent-stage39');
  await page.getByRole('button', { name: 'Create conversation', exact: true }).click();
  await expect.poll(() => fixture.created()?.agent_id).toBe('agent-stage39');
});

test('compact chat typing preserves follow and sending resumes a paused timeline', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  await projectFixture(page, true);
  await page.route('**/api/v1/chat/v2/conversations/conv-task-chat/snapshot**', (route) => route.fulfill({ json: {
    schema_version: 2, projection_version: 'composer-follow-test',
    scope: { key: 'conversation:conv-task-chat', kind: 'conversation', conversation_id: 'conv-task-chat' },
    conversation: { conversation_id: 'conv-task-chat', agent_id: 'agent-stage39', status: 'active' },
    timeline: {
      items: Array.from({ length: 30 }, (_, i) => ({
        id: `message:history-${i}`, kind: 'message', role: 'assistant', message_id: `history-${i}`,
        sort_key: `0000:${String(i).padStart(15, '0')}:000000:02:000000000`,
        source_refs: [], stable: true, content: `History paragraph ${i}.\n\n${'Conversation history. '.repeat(12)}`,
        attachments: [], partial: false,
      })), has_more_before: false, before_cursor: null,
    },
    runtime: { runtime_epoch: 'composer-follow', runtime_revision: 1, has_active_turn: false, active_turn: null, volatile_items: [], cycle_states: [] },
    queue: { messages: [], queued_count: 0 }, cursor: 'composer-follow-cursor', server_time: '',
  } }));
  await page.goto('/projects/proj_home');
  await page.getByRole('button', { name: /Kitchen renovation/ }).first().click();
  const chat = page.getByTestId('compact-conversation-chat');
  const viewport = chat.getByTestId('scoped-timeline-viewport');
  const composer = chat.getByTestId('compact-chat-composer');
  const distanceFromBottom = () => viewport.evaluate((node) => node.scrollHeight - node.scrollTop - node.clientHeight);
  await expect.poll(distanceFromBottom).toBeLessThanOrEqual(2);
  await expect(chat).toHaveAttribute('data-auto-tail', 'following');
  for (const text of ['A', 'A longer message', 'Multiple\nlines\nof\nmessage\ntext', 'Short again']) {
    await composer.fill(text);
    await expect.poll(distanceFromBottom).toBeLessThanOrEqual(2);
    await expect(chat).toHaveAttribute('data-auto-tail', 'following');
  }
  await viewport.hover();
  await page.mouse.wheel(0, -500);
  await expect(chat).toHaveAttribute('data-auto-tail', 'paused');
  await chat.getByRole('button', { name: 'Send message', exact: true }).click();
  await expect(chat).toHaveAttribute('data-auto-tail', 'following');
  await expect.poll(distanceFromBottom).toBeLessThanOrEqual(2);
});

test('Chat defaults to Web but preserves an explicit All channels selection', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  await projectFixture(page);
  await page.goto('/chat/conv-task-chat');
  await page.getByRole('button', { name: 'Expand conversation filters', exact: true }).click();
  const channel = page.locator('#conversation-channel-filter');
  await expect(channel).toHaveText('Web');
  await channel.click();
  await page.getByRole('option', { name: 'All channels', exact: true }).click();
  await expect(channel).toHaveText('All channels');
  await page.reload();
  await page.getByRole('button', { name: 'Expand conversation filters', exact: true }).click();
  await expect(channel).toHaveText('All channels');
});

test('project-only agent strip cannot open global direct chat', async ({ page }) => {
  const fixture = await projectFixture(page, false, true);
  await page.route('**/api/v1/agents?**', (route) => route.fulfill({ json: {
    items: [{ agent_id: 'agent-stage39', name: 'Butler', display_name: 'Butler',
      status: 'active', agent_type: 'primary', profiles: [], shared_via_project_ids: ['proj_home'] }],
    has_more: false, next_cursor: null,
  } }));
  let resolved = false;
  await page.route('**/api/v1/conversations/resolve', (route) => {
    resolved = true;
    return route.abort();
  });
  await page.goto('/projects/proj_home');
  await page.getByTestId('dashboard-conversations-agents').getByRole('button', { name: /Butler/ }).click();
  await page.getByRole('button', { name: 'Create conversation', exact: true }).click();
  await expect.poll(() => fixture.created()?.project_id).toBe('proj_home');
  expect(resolved).toBe(false);
  expect(new URL(page.url()).pathname).toBe('/projects/proj_home');
});

test('dashboard window preference is reused without dashboard storage', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  await projectFixture(page, true);
  await page.goto('/projects/proj_home');
  await page.getByRole('button', { name: /Release safety review/ }).first().click();
  await expect(page.getByTestId(`workspace-window-task:${TASK_ID}`)).toBeVisible();
  expect(new URL(page.url()).pathname).toBe('/projects/proj_home');
  const taskWindow = page.getByTestId(`workspace-window-task:${TASK_ID}`);
  // Place an actual background control under a point outside the window.
  await page.evaluate(() => {
    const button = document.createElement('button');
    button.id = 'background-click-probe';
    button.textContent = 'Background control';
    button.style.cssText = 'position:fixed;top:4px;left:4px;width:80px;height:30px;z-index:69';
    button.onclick = () => { button.dataset.activated = 'true'; };
    document.body.appendChild(button);
  });
  await page.mouse.click(20, 15);
  await expect(taskWindow).toHaveAttribute('data-minimized', 'true');
  await expect(page.locator('#background-click-probe')).not.toHaveAttribute('data-activated');
  const dashboardControl = page.getByTestId('workspace-dock-dashboard');
  await expect(dashboardControl).toHaveAttribute('title', 'Restore windows');
  await page.mouse.click(20, 15);
  await expect(page.locator('#background-click-probe')).toHaveAttribute('data-activated', 'true');
  await dashboardControl.click();
  await expect(taskWindow).toHaveAttribute('data-minimized', 'false');
  await page.locator('#background-click-probe').evaluate((node) => node.remove());
  const keys = await page.evaluate(() => Object.keys(localStorage));
  expect(keys.some((key) => key.startsWith('cognis-project-workspace:') && key.includes('proj_home'))).toBe(true);
  expect(keys.some((key) => key.startsWith('cognis.dashboard.workspace.v1:'))).toBe(false);
  await page.screenshot({ path: test.info().outputPath('project-window.png') });
  await page.goBack();
  await expect(page.getByTestId(`workspace-window-task:${TASK_ID}`)).toHaveCount(0);
  await page.goForward();
  await expect(page).toHaveURL(/view=task/);
  await expect(page.getByTestId(`workspace-window-task:${TASK_ID}`)).toBeVisible();
});

test('unavailable asset links are explicit and schedules cannot break project activity', async ({ page }) => {
  await projectFixture(page);
  await page.route('**/api/v1/schedules**', (route) => route.fulfill({ status: 503, json: { detail: 'Temporarily unavailable' } }));
  await page.goto('/projects/proj_home?view=artifact&item=art_removed');
  const unavailable = page.getByRole('dialog', { name: 'Resource', exact: true });
  await expect(unavailable.getByRole('alert')).toContainText('Item unavailable');
  await page.keyboard.press('Escape');
  await expect(unavailable).toBeHidden();
  await expect(page.getByRole('button', { name: /Kitchen renovation/ }).first()).toBeVisible();
  await page.getByTestId('project-tab-schedules').click();
  await expect(page.getByRole('button', { name: 'Retry', exact: true })).toBeVisible();
});

test('schedule editor and trigger stay in the project and ignore a late trigger after close', async ({ page }) => {
  await page.setViewportSize({ width: 320, height: 900 });
  await projectFixture(page);
  let updated = false;
  let release!: () => void;
  const gate = new Promise<void>((resolve) => { release = resolve; });
  await page.route('**/api/v1/schedules/sch_home', async (route) => {
    if (route.request().method() !== 'PUT') return route.fallback();
    updated = true;
    await route.fallback();
  });
  await page.route('**/api/v1/schedules/sch_home/trigger', async (route) => {
    await gate;
    await route.fulfill({ json: { task_id: TASK_ID } });
  });
  await page.goto('/projects/proj_home?view=schedule&item=sch_home&tab=schedules');
  await page.getByRole('button', { name: 'Edit schedule' }).click();
  await expect(page.getByRole('button', { name: 'Save changes' })).toBeVisible();
  await page.getByRole('button', { name: 'Save changes' }).click();
  await expect.poll(() => updated).toBe(true);
  const detail = page.getByTestId('dashboard-schedule-modal-scroll-shell');
  await expect.poll(() => detail.evaluate((node) => node.scrollWidth - node.clientWidth)).toBeLessThanOrEqual(1);
  // Accessible action labels are also usable without hover on phones.
  await detail.getByRole('button', { name: 'Run now', exact: true }).click();
  await page.getByRole('dialog', { name: /^Run ".*" now\?$/ }).getByRole('button', { name: 'Run now' }).click();
  await page.getByTestId('dashboard-schedule-modal-close').click();
  release();
  await page.waitForTimeout(150);
  expect(new URL(page.url()).pathname).toBe('/projects/proj_home');
  expect(new URL(page.url()).searchParams.get('view')).toBeNull();
});

test('sidebar title search renders matches and refreshes when the project filter changes', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  await projectFixture(page);
  await page.route('**/api/v1/projects', (route) => route.fulfill({ json: [
    { project_id: 'proj_home', name: 'Home', status: 'active', caller_role: 'owner' },
  ] }));
  const queries: { query: string | null; project: string | null }[] = [];
  await page.route('**/api/v1/conversations?**', (route) => {
    const params = new URL(route.request().url()).searchParams;
    const query = params.get('q');
    const project = params.get('project_id');
    queries.push({ query, project });
    return route.fulfill({ json: {
      items: query === 'Kitchen' ? [{
        conversation_id: 'conv-task-chat-b', agent_id: 'agent-stage39',
        title: project ? 'Kitchen project match' : 'Kitchen title match',
        project_id: project, status: 'active', context: { type: 'web' },
        user_email: 'owner@cognis.test', active_session_id: null,
      }] : [],
      cursor: null, has_more: false,
    } });
  });
  await page.goto('/chat/conv-task-chat');
  await page.getByRole('button', { name: 'Expand conversation search', exact: true }).click();
  const search = page.locator('#conversation-sidebar-search');
  await search.fill('Kitchen');
  await expect(page.getByText('Kitchen title match', { exact: true })).toBeVisible();
  expect(queries).toContainEqual({ query: 'Kitchen', project: null });

  await page.getByRole('button', { name: 'Expand conversation filters', exact: true }).click();
  await page.locator('#conversation-project-filter').selectOption('proj_home');
  await expect(page.getByText('Kitchen project match', { exact: true })).toBeVisible();
  await expect(page.getByText('Kitchen title match', { exact: true })).toHaveCount(0);
  expect(queries).toContainEqual({ query: 'Kitchen', project: 'proj_home' });

  await search.fill('No matching title');
  await expect(page.getByText('Kitchen project match', { exact: true })).toHaveCount(0);
  await expect(page.getByText('No conversations found.', { exact: true })).toBeVisible();
  await page.getByRole('button', { name: 'Clear search', exact: true }).click();
  await expect(search).toHaveValue('');
});

test('installed mobile PWA exposes conversation metadata and project editing in the sidebar', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.addInitScript(() => {
    Object.defineProperty(navigator, 'standalone', { configurable: true, value: true });
  });
  await projectFixture(page);
  const projects = ['Home', 'Work'].map((name) => ({
    project_id: `proj_${name.toLowerCase()}`, name, status: 'active', caller_role: 'owner',
    grants: [{ grantee_user_email: 'partner@cognis.test', revoked_at: null }],
  }));
  const conversation = {
    conversation_id: 'conv-task-chat', title: 'Kitchen renovation', user_email: 'owner@cognis.test',
    agent_id: 'agent-stage39', agent_profile_id: 'developer', project_id: 'proj_home',
    project_assignable: true, visibility: 'project', context: { type: 'web' },
    active_session_id: 'sess-task-chat', status: 'active', is_readonly_for_caller: false,
  };
  const mutations: Record<string, unknown>[] = [];
  await page.route('**/api/v1/projects', (route) => route.fulfill({ json: projects }));
  await page.route('**/api/v1/conversations/conv-task-chat**', (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path !== '/api/v1/conversations/conv-task-chat') return route.fallback();
    if (route.request().method() === 'PATCH') {
      const body = route.request().postDataJSON();
      mutations.push(body);
      Object.assign(conversation, body);
    }
    return route.fulfill({ json: conversation });
  });
  await page.route('**/api/v1/chat/v2/conversations/conv-task-chat/snapshot**', (route) => route.fulfill({ json: {
    schema_version: 2, projection_version: 'project-mobile-metadata',
    scope: { key: 'conversation:conv-task-chat', kind: 'conversation', conversation_id: 'conv-task-chat' },
    conversation, timeline: { items: [], has_more_before: false, before_cursor: null },
    runtime: { active_session_id: 'sess-task-chat', active_turn_id: null, active_turn_state: null, has_active_turn: false, volatile_items: [] },
    cursor: 'project-mobile-snapshot', server_time: '2026-10-04T05:00:00Z',
  } }));
  await page.goto('/chat/conv-task-chat');
  await page.getByTestId('chat-header-info').click();
  const metadata = page.getByTestId('conversation-sidebar-metadata');
  await expect(metadata).toBeVisible();
  await expect(metadata.getByTitle('Copy full session ID')).toBeVisible();
  await expect(metadata.getByTitle('Agent profile')).toContainText('developer');
  await metadata.getByTestId('conversation-project-chip').click();
  page.once('dialog', (dialog) => void dialog.accept());
  await metadata.getByRole('option', { name: 'Work' }).click();
  await expect.poll(() => mutations.some((item) => item.project_id === 'proj_work')).toBe(true);
  await expect(metadata.getByTestId('conversation-project-chip')).toContainText('Work');
  await metadata.getByTestId('conversation-visibility-project').click();
  await expect.poll(() => mutations.some((item) => item.visibility === 'private')).toBe(true);
  await expect(metadata.getByTestId('conversation-visibility-private')).toBeVisible();
  await expect.poll(() => metadata.evaluate((node) => node.scrollWidth - node.clientWidth)).toBeLessThanOrEqual(1);
  await page.screenshot({ path: test.info().outputPath('pwa-conversation-metadata.png') });
});

test('overview resource cards preview images and load author workflow outputs through the step scope', async ({ page }) => {
  await projectFixture(page);
  await page.route('**/api/v1/deliverables/dlv_project_report', (route) =>
    route.fulfill({ status: 404, json: { detail: 'Deliverable not found' } }));
  await page.route('**/api/v1/projects/proj_home/activity**', (route) => route.fulfill({ json: {
    deliverables: [{ deliverable_id: 'dlv_project_report', title: 'Renovation plan', format: 'markdown',
      step_run_id: 'step_project_report', is_readonly_for_caller: false, content_preview: 'Keep the original oak floor.' }],
    artifacts: [{ artifact_id: 'art_project_photo', filename: 'Kitchen.png', mime_type: 'image/png', size_bytes: 128 }],
  } }));
  await page.route('**/api/v1/artifacts/art_project_photo/signed-url**', (route) => route.fulfill({ json: {
    url: 'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aJ1sAAAAASUVORK5CYII=',
  } }));
  await page.goto('/projects/proj_home');
  await expect(page.getByText('Keep the original oak floor.', { exact: true })).toBeVisible();
  const thumbnail = page.getByTestId('project-artifact-thumbnail').locator('img');
  await page.getByTestId('project-artifact-thumbnail').scrollIntoViewIfNeeded();
  await expect(thumbnail).toBeVisible();
  await expect.poll(() => thumbnail.evaluate((img: HTMLImageElement) => img.naturalWidth)).toBeGreaterThan(0);
  await page.getByRole('button', { name: /Renovation plan/ }).click();
  await expect(page.locator('p').getByText('Keep the original oak floor.', { exact: true })).toBeVisible();
  expect(new URL(page.url()).pathname).toBe('/projects/proj_home');
});
