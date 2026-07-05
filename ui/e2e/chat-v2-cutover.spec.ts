/**
 * L3 Playwright browser e2e tests for the Chat v2 frontend cutover.
 *
 * Prerequisites:
 *   make e2e-up && make e2e-seed
 *
 * Run:
 *   cd ui && npx playwright test e2e/chat-v2-cutover.spec.ts
 */

import { test } from '@playwright/test';
import {
  clearScenario,
  expect,
  injectScenario,
  login,
  openOrCreateConversation,
} from './helpers';

test.beforeEach(async ({ page }) => {
  await login(page);
  await openOrCreateConversation(page);
});

test.afterEach(async () => {
  await clearScenario().catch(() => {});
});

test('completed Chat v2 timeline survives reload without stale runtime items', async ({ page }) => {
  test.setTimeout(120_000);
  await injectScenario('single-phase-stream');

  const conversationId = new URL(page.url()).pathname.split('/').pop();
  expect(conversationId).toBeTruthy();
  await page.evaluate(
    async ({ conversationId: targetConversationId, message }) => {
      const clientTxnId = `e2e_txn_${Date.now()}_${Math.random().toString(36).slice(2)}`;
      const response = await fetch(`/api/v1/chat/v2/conversations/${targetConversationId}/messages/${clientTxnId}`, {
        method: 'PUT',
        credentials: 'include',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          content: message,
          attachments: [],
          client_message_id: clientTxnId,
        }),
      });
      if (!response.ok) {
        throw new Error(`Chat v2 send failed: ${response.status} ${await response.text()}`);
      }
    },
    {
      conversationId,
      message: 'scenario:single-phase-stream',
    },
  );

  await page.waitForFunction(
    async ({ conversationId: targetConversationId, expectedText }) => {
      const response = await fetch(`/api/v1/chat/v2/conversations/${targetConversationId}/snapshot`, {
        credentials: 'include'
      });
      if (!response.ok) return false;
      const snapshot = await response.json();
      const items = snapshot.timeline?.items ?? [];
      const hasUserMessage = items.some((item: { kind?: string; role?: string; content?: string }) => (
        item.kind === 'message'
        && item.role === 'user'
        && item.content === 'scenario:single-phase-stream'
      ));
      const hasAssistantMessage = items.some((item: { kind?: string; role?: string; content?: string }) => (
        item.kind === 'message'
        && item.role === 'assistant'
        && typeof item.content === 'string'
        && item.content.includes(expectedText)
      ));
      return hasUserMessage && hasAssistantMessage;
    },
    {
      conversationId,
      expectedText: 'Hello world! This is a streaming response.'
    },
    { timeout: 90_000 },
  );

  await page.reload();
  await expect(page).toHaveURL(new RegExp(`/chat/${conversationId}$`));
  await page.waitForLoadState('networkidle');
  await expect(page.locator('[data-kind="message"][data-role="user"]')).toHaveCount(1, {
    timeout: 30_000,
  });

  const assistantMessages = page.locator('[data-kind="message"][data-role="assistant"]');
  await expect(assistantMessages).toHaveCount(1, { timeout: 30_000 });
  await expect(assistantMessages.first()).toContainText('Hello world! This is a streaming response.', {
    timeout: 30_000,
  });
  await expect(page.locator('[data-streaming="true"]')).toHaveCount(0, { timeout: 30_000 });
});
