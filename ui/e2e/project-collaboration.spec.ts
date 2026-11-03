/**
 * Project collaboration E2E: the "Home" household project shared with a
 * second user. Covers agent assignment, the project picker in chats, the
 * sidebar project filter, sharing roles, and read-only shared conversations
 * with "Continue in my chat".
 *
 * Screenshots are written to `test-results/project-collaboration/` for human
 * review (set PROJECT_E2E_SCREENSHOT_DIR to override).
 *
 * Prerequisites: make e2e-up && make e2e-seed
 */

import { test, type APIRequestContext, type Browser, type Page } from '@playwright/test';

import { ADMIN_EMAIL, ADMIN_PASSWORD, expect, injectScenario, login } from './helpers';

const SHOT_DIR = process.env.PROJECT_E2E_SCREENSHOT_DIR ?? 'test-results/project-collaboration';
const PARTNER_EMAIL = 'partner@cognis-e2e.localdev.me';
const PARTNER_PASSWORD = 'cognis-partner-pass-1';
const RUN = Date.now().toString(36);

async function shot(page: Page, name: string): Promise<void> {
  await page.waitForTimeout(400);
  await page.screenshot({ path: `${SHOT_DIR}/${name}.png`, fullPage: false });
}

async function api<T>(page: Page, method: string, path: string, body?: unknown): Promise<T> {
  return page.evaluate(
    async ({ method: m, path: p, body: b }) => {
      const response = await fetch(p, {
        method: m,
        credentials: 'include',
        headers: { 'Content-Type': 'application/json' },
        body: b === undefined ? undefined : JSON.stringify(b),
      });
      const text = await response.text();
      if (!response.ok) throw new Error(`${m} ${p} -> ${response.status} ${text}`);
      return text ? JSON.parse(text) : null;
    },
    { method, path, body },
  ) as Promise<T>;
}

async function sendAndWait(page: Page, conversationId: string, content: string): Promise<void> {
  await injectScenario('single-phase-stream');
  const txn = `e2e_txn_${Date.now()}_${Math.random().toString(36).slice(2)}`;
  await api(page, 'PUT', `/api/v1/chat/v2/conversations/${conversationId}/messages/${txn}`, {
    content,
    attachments: [],
    client_message_id: txn,
  });
  await page.waitForFunction(
    async (id) => {
      const response = await fetch(`/api/v1/chat/v2/conversations/${id}/snapshot`, { credentials: 'include' });
      if (!response.ok) return false;
      const snapshot = await response.json();
      const items = snapshot.timeline?.items ?? [];
      if (!items.some((item: { kind?: string; role?: string }) => item.kind === 'message' && item.role === 'assistant')) {
        return false;
      }
      const detail = await fetch(`/api/v1/conversations/${id}?include_state=false`, { credentials: 'include' });
      return detail.ok && (await detail.json()).has_active_turn === false;
    },
    conversationId,
    { timeout: 60_000, polling: 500 },
  );
}

async function loginAs(browser: Browser, email: string, password: string): Promise<Page> {
  const context = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  const page = await context.newPage();
  await page.goto('/');
  await page.fill('[data-testid="email"], input[type="email"]', email);
  await page.fill('[data-testid="password"], input[type="password"]', password);
  await page.locator('[data-testid="login-submit"], button[type="submit"]').first().click();
  await expect(page.getByTestId('control-center')).toBeVisible({ timeout: 30_000 });
  return page;
}

async function ensurePartner(request: APIRequestContext, page: Page): Promise<void> {
  void request;
  await page.evaluate(
    async ({ email, password }) => {
      const existing = await fetch(`/api/v1/admin/users/${encodeURIComponent(email)}`, { credentials: 'include' });
      if (existing.ok) {
        await fetch(`/api/v1/admin/users/${encodeURIComponent(email)}`, {
          method: 'PATCH',
          credentials: 'include',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ password }),
        });
        return;
      }
      const response = await fetch('/api/v1/admin/users', {
        method: 'POST',
        credentials: 'include',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ email, name: 'Partner', password, role: 'user' }),
      });
      if (!response.ok) throw new Error(`create partner -> ${response.status} ${await response.text()}`);
    },
    { email: PARTNER_EMAIL, password: PARTNER_PASSWORD },
  );
}

test.describe.configure({ mode: 'serial' });

test('Home project: owner assigns agent, shares, and partner reads and continues', async ({ page, browser, request }) => {
  test.setTimeout(300_000);
  await page.setViewportSize({ width: 1440, height: 900 });
  await login(page);
  void ADMIN_PASSWORD;

  // --- Owner setup through the public API -------------------------------
  await ensurePartner(request, page);
  const template = await api<Record<string, unknown>>(page, 'GET', '/api/v1/agents/e2e-test-agent');
  const butlerId = `butler-${RUN}`;
  await api(page, 'POST', '/api/v1/agents', {
    agent_id: butlerId,
    name: 'Butler',
    description: 'Household assistant: reconstruction, shopping, home automation.',
    system_prompt: 'You help with the household.',
    llm_config: template.llm_config,
    tools: template.tools,
    capabilities: template.capabilities,
    execution: template.execution,
  });
  await api(page, 'POST', `/api/v1/agents/${butlerId}/activate`);
  const home = await api<{ project_id: string }>(page, 'POST', '/api/v1/projects', {
    name: `Home ${RUN}`,
    description: 'Household, reconstruction, budget, and home automation.',
    instructions: 'Prefer Czech suppliers. Budget changes need both of us to agree.',
  });
  await api(page, 'POST', '/api/v1/projects', { name: `Work ${RUN}`, description: 'Contract work.' });

  // Owner assigns Butler through the Agents tab UI.
  await page.goto(`/projects/${home.project_id}?tab=agents`);
  const assignForm = page.getByTestId('project-assign-agent');
  await expect(assignForm).toBeVisible();
  await assignForm.locator('select').first().selectOption(butlerId);
  await assignForm.getByLabel('Default project').check();
  await assignForm.getByRole('button', { name: 'Assign' }).click();
  await expect(page.getByTestId('project-agent-row')).toContainText('Butler');
  await expect(page.getByTestId('project-agent-row')).toContainText('default project');
  await shot(page, '01-owner-project-agents-tab');

  // Owner invites the partner as a member through the Sharing tab.
  await page.getByTestId('project-tab-sharing').click();
  const invite = page.getByTestId('project-invite');
  await invite.getByPlaceholder('user@example.com').fill(PARTNER_EMAIL);
  await invite.getByLabel('Role').selectOption('member');
  await invite.getByRole('button', { name: 'Invite' }).click();
  await expect(page.getByTestId('project-members')).toContainText(PARTNER_EMAIL);
  await shot(page, '02-owner-project-sharing-roles');

  // New chat dialog preselects the project; Butler defaults to Home.
  await page.goto(`/chat/new?project_id=${home.project_id}&agent_id=${butlerId}`);
  await expect(page.getByTestId('new-chat-project')).toBeVisible();
  await shot(page, '03-new-chat-project-picker');
  await page.getByRole('button', { name: 'Create conversation' }).click();
  await page.waitForURL(/\/chat\/conv_/);
  const sharedConversationId = new URL(page.url()).pathname.split('/').pop() as string;
  await api(page, 'PATCH', `/api/v1/conversations/${sharedConversationId}`, { title: 'Kitchen reconstruction plan' });
  await sendAndWait(page, sharedConversationId, 'scenario:single-phase-stream');
  await page.reload();
  await expect(page.getByTestId('conversation-project-chip')).toContainText(`Home ${RUN}`);
  await expect(page.getByTestId('conversation-visibility-project')).toBeVisible();
  await shot(page, '04-owner-chat-project-chip-shared');

  // Project chip dropdown to move the chat between projects.
  await page.getByTestId('conversation-project-chip').click();
  await shot(page, '05-owner-chat-change-project-menu');
  await page.keyboard.press('Escape');
  await page.getByTestId('conversation-project-chip').click();

  // Private chat in the same project stays hidden from the partner.
  const privateChat = await api<{ conversation_id: string }>(page, 'POST', '/api/v1/conversations', {
    agent_id: butlerId,
    project_id: home.project_id,
    visibility: 'private',
    title: 'Birthday surprise (private)',
    context: { type: 'web', ref: null, platform_data: {}, memory_labels: {} },
  });
  void privateChat;

  // Sidebar project filter.
  await page.goto(`/chat/${sharedConversationId}`);
  const projectFilter = page.locator('#conversation-project-filter');
  await expect(page.locator('#conversation-agent-filter').or(page.getByRole('button', { name: 'Expand conversation filters' })).first()).toBeAttached({ timeout: 30_000 });
  await expect(async () => {
    const expandFilters = page.getByRole('button', { name: 'Expand conversation filters' });
    if (await expandFilters.count() > 0) await expandFilters.click();
    await expect(projectFilter).toBeVisible({ timeout: 2_000 });
  }).toPass({ timeout: 30_000 });
  await projectFilter.selectOption(home.project_id);
  await page.waitForTimeout(1_000);
  await shot(page, '06-sidebar-project-filter');
  await projectFilter.selectOption('');

  // Project overview from the owner side.
  await page.goto(`/projects/${home.project_id}`);
  await expect(page.getByTestId('project-role')).toHaveText('Owner');
  await shot(page, '07-owner-project-overview');

  // --- Partner (member) ---------------------------------------------------
  const partner = await loginAs(browser, PARTNER_EMAIL, PARTNER_PASSWORD);
  await partner.goto('/projects');
  await expect(partner.getByText(`Home ${RUN}`)).toBeVisible();
  await shot(partner, '08-partner-projects-list');

  await partner.goto(`/projects/${home.project_id}?tab=conversations`);
  await expect(partner.getByTestId('project-role')).toHaveText('Member');
   const rows = partner.getByTestId(/^dashboard-conversation-row-/);
  await expect(rows.filter({ hasText: 'Kitchen reconstruction plan' })).toHaveCount(1);
  await expect(rows.filter({ hasText: 'Birthday surprise' })).toHaveCount(0);
  await shot(partner, '09-partner-project-conversations');

  // Readers receive no live pushes; wait until the reply is durable for them.
  await partner.waitForFunction(
    async (id) => {
      const response = await fetch(`/api/v1/chat/v2/conversations/${id}/snapshot`, { credentials: 'include' });
      if (!response.ok) return false;
      const items = (await response.json()).timeline?.items ?? [];
      return items.some((item: { kind?: string; role?: string }) => item.kind === 'message' && item.role === 'assistant');
    },
    sharedConversationId,
    { timeout: 30_000, polling: 500 },
  );
  await partner.goto(`/chat/${sharedConversationId}`);
  const banner = partner.getByTestId('shared-conversation-banner');
  await expect(banner).toBeVisible({ timeout: 30_000 });
  await expect(banner).toContainText(ADMIN_EMAIL);
  await expect(partner.locator('[data-kind="message"][data-role="assistant"]').first()).toBeVisible({ timeout: 30_000 });
  await shot(partner, '10-partner-shared-chat-readonly');

  await banner.getByRole('button', { name: 'Continue in my chat' }).click();
  await partner.waitForURL((url) => url.pathname.startsWith('/chat/conv_') && !url.pathname.endsWith(sharedConversationId));
  await expect(partner.getByTestId('conversation-project-chip')).toContainText(`Home ${RUN}`);
  await shot(partner, '11-partner-continued-own-chat');

  // Partner settings tab is read-only for members.
  await partner.goto(`/projects/${home.project_id}?tab=settings`);
  await shot(partner, '12-partner-settings-readonly');

  // Owner's agent page lists project assignments.
  await page.goto(`/agents/${butlerId}`);
  await page.getByRole('tab', { name: 'Collaboration' }).click();
  await expect(page.getByTestId('agent-projects')).toBeVisible();
  await page.getByTestId('agent-projects').scrollIntoViewIfNeeded();
  await shot(page, '13-owner-agent-projects');

  // /project slash command in a topic chat.
  await page.goto(`/chat/${sharedConversationId}`);
  const composer = page.locator('textarea[placeholder*="Message"], textarea[placeholder*="message"]').first();
  await composer.click();
  await composer.pressSequentially('/project ', { delay: 60 });
  await page.waitForTimeout(1_200);
  await shot(page, '14-project-slash-command-suggestions');
  await composer.fill('/project');
  await page.keyboard.press('Escape');
  await page.getByRole('button', { name: 'Send' }).click();
  await expect(page.getByText(/Project: Home/)).toBeVisible({ timeout: 15_000 });
  await shot(page, '15-project-slash-command-status');

  await partner.context().close();
});
