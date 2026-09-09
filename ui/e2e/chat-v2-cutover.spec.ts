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

let conversationId = '';

test.beforeEach(async ({ page }) => {
  await login(page);
  await openOrCreateConversation(page);
  conversationId = new URL(page.url()).pathname.split('/').pop() ?? '';
  if (!conversationId) throw new Error('E2E conversation ID is missing');
});

test.afterEach(async () => {
  await clearScenario().catch(() => {});
});

test('settles the original timeline after a rapid A to B to A route reversal', async ({ page }) => {
  const targetTitle = `Rapid switch target ${Date.now()}`;
  const targetConversationId = await page.evaluate(async (title) => {
    const response = await fetch('/api/v1/conversations', {
      method: 'POST',
      credentials: 'include',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        agent_id: 'e2e-test-agent',
        title,
        context: {
          type: 'web',
          ref: null,
          platform_data: {},
          memory_labels: {},
        },
      }),
    });
    if (!response.ok) throw new Error(`Failed to create target conversation: ${response.status}`);
    return (await response.json()).conversation_id as string;
  }, targetTitle);
  await page.reload();

  let releaseTargetDetail!: () => void;
  const targetDetailReleased = new Promise<void>((resolve) => {
    releaseTargetDetail = resolve;
  });
  let observeTargetDetail!: () => void;
  const targetDetailObserved = new Promise<void>((resolve) => {
    observeTargetDetail = resolve;
  });
  await page.route(
    `**/api/v1/conversations/${targetConversationId}?**`,
    async (route) => {
      observeTargetDetail();
      await targetDetailReleased;
      await route.continue();
    },
  );

  const targetLink = page.locator(`a[href="/chat/${targetConversationId}"]`).first();
  const originalLink = page.locator(`a[href="/chat/${conversationId}"]`).first();
  await expect(targetLink).toBeVisible();
  await targetLink.click({ noWaitAfter: true });
  await targetDetailObserved;
  await originalLink.click({ noWaitAfter: true });
  releaseTargetDetail();

  await expect(page).toHaveURL(new RegExp(`/chat/${conversationId}$`));
  const timeline = page.getByTestId('timeline-viewport');
  await expect(timeline).toHaveCSS('visibility', 'visible');
  await expect(timeline).toHaveCSS('pointer-events', 'auto');
});

test('keeps the settled mobile chat header and composer above the keyboard', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.reload();
  // The backdrop shares this accessible name; use the panel's visible action.
  const closeConversationList = page.getByRole('button', { name: 'Close conversation list' })
    .filter({ hasText: 'Close' });
  if (await closeConversationList.isVisible().catch(() => false)) {
    await closeConversationList.click();
  }

  const chat = page.getByTestId('chat-main');
  const header = page.getByTestId('chat-header');
  const timeline = page.getByTestId('timeline-viewport');
  const composer = page.getByLabel('Chat message');
  await expect(chat).toBeVisible();
  await expect(header).toBeVisible();
  await expect(timeline).toHaveCSS('visibility', 'visible');
  await expect(composer).toBeVisible();

  await page.evaluate(() => {
    const probe = document.createElement('input');
    probe.dataset.testid = 'keyboard-focus-probe';
    probe.style.position = 'fixed';
    probe.style.width = '1px';
    probe.style.height = '1px';
    probe.style.opacity = '0';
    document.body.append(probe);
    probe.focus();
    const viewport = window.visualViewport;
    if (!viewport) throw new Error('visualViewport is required for keyboard acceptance');
    Object.defineProperty(viewport, 'height', { configurable: true, get: () => 520 });
    Object.defineProperty(viewport, 'offsetTop', { configurable: true, get: () => 0 });
    viewport.dispatchEvent(new Event('resize'));
  });

  await expect.poll(() => page.evaluate(() => document.documentElement.dataset.keyboard))
    .toBe('open');
  const geometry = await page.evaluate(() => {
    const chatNode = document.querySelector<HTMLElement>('[data-testid="chat-main"]');
    const headerNode = document.querySelector<HTMLElement>('[data-testid="chat-header"]');
    const composerNode = document.querySelector<HTMLElement>('[aria-label="Chat message"]');
    const timelineNode = document.querySelector<HTMLElement>('[data-testid="timeline-viewport"]');
    if (!chatNode || !headerNode || !composerNode || !timelineNode) {
      throw new Error('Mobile chat geometry nodes are missing');
    }
    const chatBox = chatNode.getBoundingClientRect();
    const headerBox = headerNode.getBoundingClientRect();
    const composerBox = composerNode.getBoundingClientRect();
    const visualBottom = (window.visualViewport?.offsetTop ?? 0)
      + (window.visualViewport?.height ?? window.innerHeight);
    return {
      chatTop: chatBox.top,
      headerTop: headerBox.top,
      headerBottom: headerBox.bottom,
      composerTop: composerBox.top,
      composerBottom: composerBox.bottom,
      visualBottom,
      timelineVisibility: getComputedStyle(timelineNode).visibility,
      pageOverflow: document.documentElement.scrollHeight
        - document.documentElement.clientHeight,
    };
  });
  expect(geometry.headerTop).toBeGreaterThanOrEqual(geometry.chatTop);
  expect(geometry.headerBottom).toBeLessThanOrEqual(geometry.composerTop);
  expect(geometry.composerBottom).toBeLessThanOrEqual(geometry.visualBottom + 1);
  expect(geometry.timelineVisibility).toBe('visible');
  expect(geometry.pageOverflow).toBeLessThanOrEqual(1);
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
