import { expect, test } from '@playwright/test';
import { installTaskCockpitFixture } from './task-cockpit-fixture';
test.use({ serviceWorkers: 'block', hasTouch: true });

for (const canContinue of [false, true]) {
  test(`shared chat badge, filter and composer with continuation ${canContinue}`, async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await installTaskCockpitFixture(page);
    await page.route('**/api/v1/user-preferences', (route) => route.fulfill({ json: {
      display: { theme: 'dark', language: 'en', conversation_project_badge: canContinue ? 'monochrome' : 'name' },
    } }));
    await page.route('**/api/auth/me', (route) => route.fulfill({ json: { email: 'owner@cognis.test', role: 'user', name: 'Owner' } }));
    await page.route('**/api/health**', (route) => route.fulfill({ json: { status: 'ok', providers: {} } }));
    await page.route('**/api/v1/skills', (route) => route.fulfill({ json: [] }));
    await page.routeWebSocket('**/api/ws', (socket) => {
      socket.send(JSON.stringify({ type: 'authenticated' }));
      socket.onMessage((message) => {
        if (JSON.parse(String(message)).type === 'ping') socket.send(JSON.stringify({ type: 'pong' }));
      });
    });
    const conversation = {
      conversation_id: 'conv-task-chat', agent_id: 'agent-stage39', user_email: 'colleague@cognis.test',
      context: { type: 'web', ref: null, platform_data: {} }, title: 'Native Cognis app development strategy',
      status: 'active', is_readonly_for_caller: true, can_continue: canContinue,
      agent_display_name: 'LaForge', agent_avatar_url: 'data:image/svg+xml,' + encodeURIComponent('<svg xmlns="http://www.w3.org/2000/svg" width="64" height="64"><rect width="64" height="64" rx="32" fill="#164e63"/><text x="32" y="42" text-anchor="middle" fill="#7dd3fc" font-size="28">L</text></svg>'),
      project_id: 'proj_cognis', project_name: 'Cognis', project_color: '#38bdf8',
      project_icon: 'cognis',
      created_at: '2026-10-16T19:00:00Z', updated_at: '2026-10-16T19:00:00Z',
      last_message_at: '2026-10-16T19:00:00Z', has_active_turn: false, visibility: 'project',
    };
    const items = [
      { ...conversation, conversation_id: 'conv-own', is_readonly_for_caller: false, starred_at: '2026-10-16T19:00:00Z', title: 'Cognis project CRUD & extended usage' },
      conversation,
      { ...conversation, conversation_id: 'conv-gallery', title: 'Compare fictional exhibit captions', project_id: 'proj_gallery', project_name: 'Gallery', project_color: '#fbbf24' },
      { ...conversation, conversation_id: 'conv-catalog', title: 'Prepare a museum catalogue sample', project_id: 'proj_museum', project_name: 'Museum', project_color: '#a78bfa' },
      { ...conversation, conversation_id: 'conv-free', is_readonly_for_caller: false, title: 'Provider fallback design and project routing' },
    ];
    let includeShared = true;
    let projectFilter: string | null = null;
    await page.route('**/api/v1/conversations/sidebar**', (route) => {
      includeShared = new URL(route.request().url()).searchParams.get('include_shared') === 'true';
      projectFilter = new URL(route.request().url()).searchParams.get('project_id');
      return route.fulfill({ json: { agents: [{ agent_id: 'agent-stage39', name: 'LaForge', display_name: 'LaForge', status: 'active' }], agent_direct_chats: [], context_types: ['web'],
        conversations: { items: items.filter((row) => (includeShared || !row.is_readonly_for_caller) && (!projectFilter || row.project_id === projectFilter)), cursor: null, has_more: false },
        active_conversation_ids: [], sidebar_revision: 'shared-demo', synced_at: '2026-10-16T19:00:00Z', projection_mode: 'snapshot',
      } });
    });
    await page.route('**/api/v1/conversations/conv-task-chat', (route) => route.fulfill({ json: conversation }));
    await page.route('**/api/v1/conversations/conv-task-chat?**', (route) => route.fulfill({ json: conversation }));
    const ownConversation = items[4];
    await page.route(/\/api\/v1\/conversations\/conv-free(?:\?|$)/, (route) => route.fulfill({ json: ownConversation }));
    await page.route('**/api/v1/chat/v2/conversations/conv-task-chat/snapshot', (route) => route.fulfill({ json: {
      schema_version: 2, projection_version: 'shared', scope: { key: 'conversation:conv-task-chat', kind: 'conversation', conversation_id: 'conv-task-chat' },
      conversation, timeline: { items: [], has_more_before: false, before_cursor: null },
      runtime: { active_session_id: null, active_turn_id: null, active_turn_state: null, has_active_turn: false, volatile_items: [] },
      cursor: 'shared', server_time: '2026-10-16T19:00:00Z',
    } }));
    await page.goto('/chat/conv-task-chat');
    await expect(page.getByTestId('shared-conversation-banner')).toBeVisible();
    expect(await page.getByTestId('shared-conversation-banner').evaluate((node) => node.getBoundingClientRect().height)).toBeLessThanOrEqual(36);
    await expect(page.getByRole('button', { name: 'Continue in my chat' })).toHaveCount(canContinue ? 1 : 0);
    await page.screenshot({ path: test.info().outputPath(`read-only-composer-${canContinue}.png`) });
    await page.getByRole('button', { name: 'Open conversations' }).click();
    await expect.poll(async () => (await page.getByTestId('sidebar-conversation-row-conv-task-chat').boundingBox())?.x ?? -1).toBeGreaterThanOrEqual(0);
    await expect(page.getByTestId('conversation-project-badge').first()).toBeVisible();
    const row = page.getByTestId('sidebar-conversation-row-conv-task-chat');
    await expect(row.locator('a p')).toHaveText(conversation.title);
    const indicators = row.getByTestId('sidebar-row-indicators');
    await expect(indicators.getByTestId('conversation-project-badge')).toBeVisible();
    await expect(indicators.getByLabel('Read-only', { exact: true })).toHaveCount(0);
    const titleBox = await row.locator('a p').boundingBox();
    const badgeBox = await indicators.getByTestId('conversation-project-badge').boundingBox();
    expect(badgeBox!.x).toBeGreaterThanOrEqual(titleBox!.x + titleBox!.width);
    await expect(row.locator('img')).toBeVisible();
    expect(await row.evaluate((node) => node.getBoundingClientRect().height)).toBeLessThanOrEqual(70);
    await page.screenshot({ path: test.info().outputPath('shared-chat-project-badges.png') });
    const ownRow = page.getByTestId('sidebar-conversation-row-conv-own');
    const ownBadge = await ownRow.getByTestId('conversation-project-badge').boundingBox();
    expect(Math.abs(ownBadge!.x + ownBadge!.width / 2 - badgeBox!.x - badgeBox!.width / 2)).toBeLessThan(1);
    const star = await ownRow.getByRole('button', { name: 'Unstar conversation' }).boundingBox();
    expect(Math.abs(ownBadge!.x + ownBadge!.width / 2 - star!.x - star!.width / 2)).toBeLessThan(1);
    expect(star!.y).toBeGreaterThanOrEqual(ownBadge!.y + ownBadge!.height);
    const badge = indicators.getByTestId('conversation-project-badge');
    await badge.click();
    await expect.poll(() => projectFilter).toBe('proj_cognis');
    await expect(badge).toHaveAttribute('aria-pressed', 'true');
    await expect(badge).not.toHaveClass(/ring-1|ring-sky-400/);
    await expect(page.getByTestId('sidebar-conversation-row-conv-gallery')).toHaveCount(0);
    expect(new URL(page.url()).pathname).toBe('/chat/conv-task-chat');
    await badge.click();
    await expect.poll(() => projectFilter).toBeNull();
    await expect(page.getByTestId('sidebar-conversation-row-conv-gallery')).toBeVisible();
    await page.getByRole('button', { name: 'Expand conversation filters' }).click();
    await page.getByLabel('Shared with me', { exact: true }).uncheck();
    await expect.poll(() => includeShared).toBe(false);
    await expect(row).toHaveCount(0);
    const inactiveRow = page.getByTestId('sidebar-conversation-row-conv-free');
    await expect(inactiveRow.getByRole('button', { name: 'Star conversation', exact: true })).toHaveCount(0);
    const geometry = () => inactiveRow.evaluate((node) => {
      const row = node.getBoundingClientRect();
      return [node.querySelector('a p')!, node.querySelector('[data-testid="conversation-project-badge"]')!]
        .map((element) => {
          const rect = element.getBoundingClientRect();
          return { x: rect.x - row.x, y: rect.y - row.y, width: rect.width, height: rect.height };
        }).concat([{ x: 0, y: 0, width: row.width, height: row.height }]);
    });
    const before = await geometry();
    await inactiveRow.getByRole('link').tap();
    await expect(page).toHaveURL(/\/chat\/conv-free(?:\?|$)/);
    await page.getByRole('button', { name: 'Open conversations' }).click();
    await expect(inactiveRow.getByRole('button', { name: 'Star conversation', exact: true })).toBeVisible();
    await expect.poll(geometry).toEqual(before);
  });
}
