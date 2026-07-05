/**
 * Shared helpers for L3 Playwright browser e2e tests.
 *
 * Extracted so multiple specs (timeline.spec.ts, scroll-stability.spec.ts)
 * share one implementation of login / scenario injection / send / wait.
 *
 * Prerequisites: make e2e-up && make e2e-seed
 */

import { expect, type Page } from '@playwright/test';

export const ADMIN_EMAIL =
  process.env.COGNIS_LOCAL_ADMIN_EMAIL ?? process.env.COGNIS_E2E_ADMIN_EMAIL ?? 'admin@cognis-e2e.localdev.me';
export const ADMIN_PASSWORD =
  process.env.COGNIS_LOCAL_ADMIN_PASSWORD ?? process.env.COGNIS_E2E_ADMIN_PASSWORD ?? 'cognis-local-admin';
export const MOCK_LLM_URL = process.env.MOCK_LLM_URL ?? 'http://localhost:8090';

export async function login(page: Page): Promise<void> {
  await page.goto('/');
  await page.waitForURL(/\/(login|setup|chat)/);

  if (page.url().includes('/login')) {
    await page.fill('[data-testid="email"], input[type="email"]', ADMIN_EMAIL);
    await page.fill('[data-testid="password"], input[type="password"]', ADMIN_PASSWORD);
    await page.click('[data-testid="login-submit"], button[type="submit"]');
    await page.waitForURL(/\/chat/);
  }
}

export async function injectScenario(scenarioId: string): Promise<void> {
  const resp = await fetch(`${MOCK_LLM_URL}/__mock/active`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ id: scenarioId }),
  });
  if (!resp.ok) {
    throw new Error(`Failed to inject scenario ${scenarioId}: ${resp.status}`);
  }
}

export async function clearScenario(): Promise<void> {
  await fetch(`${MOCK_LLM_URL}/__mock/active`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ id: null }),
  });
}

export async function openOrCreateConversation(page: Page): Promise<void> {
  // Do not use `/chat` as setup plumbing. The bare chat route resolves the
  // last-opened conversation and may create/remember an empty fallback
  // conversation on a cold stack, which can race the test-created
  // conversation and leave reload assertions on an empty shell.
  await page.goto('/agents');
  await page.waitForLoadState('networkidle');

  const conversationId = await page.evaluate(async () => {
    const response = await fetch('/api/v1/conversations', {
      method: 'POST',
      credentials: 'include',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        agent_id: 'e2e-test-agent',
        title: `E2E Chat ${Date.now()}`,
        context: {
          type: 'web',
          ref: null,
          platform_data: {},
          memory_labels: {},
        },
      }),
    });
    if (!response.ok) {
      throw new Error(`Failed to create E2E conversation: ${response.status} ${await response.text()}`);
    }
    const payload = await response.json();
    return payload.conversation_id as string;
  });
  const conversationUrl = `/chat/${conversationId}`;
  let navigationError: unknown;
  for (let attempt = 0; attempt < 3; attempt += 1) {
    try {
      await page.goto(conversationUrl, { waitUntil: 'domcontentloaded', timeout: 30_000 });
      navigationError = null;
      break;
    } catch (error) {
      navigationError = error;
      await page.waitForTimeout(250);
    }
  }
  if (navigationError) {
    throw navigationError;
  }
  await expect(page).toHaveURL(new RegExp(`/chat/${conversationId}$`));
  await page.waitForLoadState('networkidle');

  const composer = page.locator('textarea[placeholder*="message"], textarea[placeholder*="Message"]').first();
  await expect(composer).toBeVisible();
  await page.waitForFunction(
    async (targetConversationId) => {
      const response = await fetch(`/api/v1/chat/v2/conversations/${targetConversationId}/snapshot`, {
        credentials: 'include',
      });
      if (!response.ok) return false;
      const snapshot = await response.json();
      return snapshot.conversation?.conversation_id === targetConversationId;
    },
    conversationId,
    { timeout: 30_000 },
  );
}

export async function sendMessage(page: Page, message: string): Promise<void> {
  const composer = page
    .locator('textarea[placeholder*="message"], textarea[placeholder*="Message"]')
    .first();
  await composer.fill(message);
  await expect(page.getByRole('button', { name: 'Send' })).toBeVisible();
  await page.getByRole('button', { name: 'Send' }).click();
  await expect(page.locator('[data-kind="message"][data-role="user"]').filter({ hasText: message })).toHaveCount(1);
}

export async function waitForTurnComplete(page: Page, timeout = 30_000): Promise<void> {
  await page.waitForFunction(
    () => (
      document.querySelectorAll('[data-streaming="true"]').length > 0
      || document.querySelectorAll(
        '[data-kind="message"][data-role="assistant"], [data-kind="thinking"], [data-kind="tool_call"]'
      ).length > 0
    ),
    undefined,
    { timeout },
  );
  await page.waitForFunction(
    () => document.querySelectorAll('[data-streaming="true"]').length === 0,
    undefined,
    { timeout },
  );
}

export { expect };
