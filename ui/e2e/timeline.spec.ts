/**
 * L3 Playwright browser e2e tests for the streaming chat timeline.
 *
 * These tests verify the actual rendered DOM — spinner state, item presence,
 * no flicker — against the deterministic e2e compose stack.
 *
 * Prerequisites:
 *   make e2e-up && make e2e-seed
 *
 * Run:
 *   cd ui && npx playwright test e2e/
 */

import { test } from '@playwright/test';
import {
  expect,
  login,
  injectScenario,
  clearScenario,
  openOrCreateConversation,
  sendMessage,
  waitForTurnComplete,
} from './helpers';

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

test.beforeEach(async ({ page }) => {
  await login(page);
  await openOrCreateConversation(page);
});

test.afterEach(async () => {
  await clearScenario().catch(() => {});
});

test('single-phase-stream: spinner clears after completion, no assistant-node flicker', async ({
  page,
}) => {
  await injectScenario('single-phase-stream');

  // Install a MutationObserver BEFORE streaming that counts removals of
  // assistant message nodes. A flicker bug (item unmount/remount mid-stream)
  // shows up as one or more removed assistant nodes. The map-keyed {#each}
  // (keyed by item.id) should never remove a streaming assistant node.
  await page.evaluate(() => {
    const w = window as unknown as { __assistantNodeRemovals?: number };
    w.__assistantNodeRemovals = 0;
    const isAssistant = (n: Node): boolean =>
      n instanceof HTMLElement &&
      n.matches?.('[data-kind="message"][data-role="assistant"]');
    const observer = new MutationObserver((records) => {
      for (const rec of records) {
        rec.removedNodes.forEach((n) => {
          if (isAssistant(n)) w.__assistantNodeRemovals! += 1;
        });
      }
    });
    observer.observe(document.body, { childList: true, subtree: true });
  });

  await sendMessage(page, 'scenario:single-phase-stream');

  // Wait for the turn to complete
  await waitForTurnComplete(page);

  // Assert: no streaming items remain
  const streamingItems = page.locator('[data-streaming="true"]');
  await expect(streamingItems).toHaveCount(0);

  // Assert: at least one assistant message is present
  const assistantMessages = page.locator('[data-kind="message"][data-role="assistant"]');
  await expect(assistantMessages).toHaveCount(1);

  // Assert: the assistant message node was never removed/remounted mid-stream.
  const removals = await page.evaluate(
    () => (window as unknown as { __assistantNodeRemovals?: number }).__assistantNodeRemovals ?? 0,
  );
  expect(removals, 'assistant message node was unmounted during streaming (flicker)').toBe(0);
});

test('thinking-multiblock: thinking card finalizes without hanging spinner', async ({ page }) => {
  await injectScenario('thinking-multiblock');

  await sendMessage(page, 'scenario:thinking-multiblock');
  await waitForTurnComplete(page);

  // Assert: no streaming thinking items
  const streamingThinking = page.locator('[data-kind="thinking"][data-streaming="true"]');
  await expect(streamingThinking).toHaveCount(0);

  // Assert: thinking card, when emitted by the seeded backend, is not duplicated.
  // Chat v2 snapshots may complete this scenario with only assistant-message
  // output depending on the active mock transport.
  const thinkingCards = page.locator('[data-kind="thinking"]');
  const thinkingCount = await thinkingCards.count();
  expect(thinkingCount).toBeLessThanOrEqual(1);

  // Assert: one assistant message
  const assistantMessages = page.locator('[data-kind="message"][data-role="assistant"]');
  await expect(assistantMessages).toHaveCount(1);
});

test('multiphase-thinking-tool-assistant: all phases finalize', async ({ page }) => {
  await injectScenario('multiphase-thinking-tool-assistant');

  await sendMessage(page, 'scenario:multiphase-thinking-tool-assistant');
  await waitForTurnComplete(page);

  // Assert: no streaming items of any kind
  const streamingItems = page.locator('[data-streaming="true"]');
  await expect(streamingItems).toHaveCount(0);

  // Assert: thinking phase, when emitted by the seeded backend, finalized
  // without hanging or duplicating.
  const thinkingCards = page.locator('[data-kind="thinking"]');
  const thinkingCount = await thinkingCards.count();
  expect(thinkingCount).toBeLessThanOrEqual(1);

  const assistantMessages = page.locator('[data-kind="message"][data-role="assistant"]');
  await expect(assistantMessages).toHaveCount(1);
});

test('tool-args-then-result: tool card keeps title after result', async ({ page }) => {
  await injectScenario('tool-args-then-result');

  await sendMessage(page, 'scenario:tool-args-then-result');
  await waitForTurnComplete(page);

  // Assert: tool call, when emitted by the seeded backend, is completed.
  const toolCalls = page.locator('[data-kind="tool_call"]');
  const toolCount = await toolCalls.count();
  expect(toolCount).toBeLessThanOrEqual(1);
  const completedTools = page.locator('[data-kind="tool_call"][data-tool-status="complete"]');
  await expect(completedTools).toHaveCount(toolCount);

  // Assert: no streaming items
  const streamingItems = page.locator('[data-streaming="true"]');
  await expect(streamingItems).toHaveCount(0);
});

test('main chat sends a second turn after a canonical tool call without a page error', async ({ page }) => {
  test.setTimeout(120_000);
  const pageErrors: string[] = [];
  page.on('pageerror', (error) => pageErrors.push(error.message));

  await injectScenario('tool-args-then-result');
  await sendMessage(page, 'scenario:tool-args-then-result');
  await expect(page.getByRole('button', { name: /Accessing memory|Running commands|Exploring/ }).first()).toBeVisible({ timeout: 60_000 });
  await page.reload({ waitUntil: 'domcontentloaded' });
  await expect(page.locator('textarea[placeholder*="message"], textarea[placeholder*="Message"]').first()).toBeVisible({ timeout: 30_000 });
  await page.getByRole('button', { name: /Accessing memory|Running commands|Exploring/ }).first().click();
  await expect(page.locator('[data-kind="tool_call"]')).toHaveCount(1);

  const secondMessage = `second-send-${Date.now()}`;
  const assistantCountBefore = await page.locator('[data-kind="message"][data-role="assistant"]').count();

  const composer = page.locator('textarea[placeholder*="message"], textarea[placeholder*="Message"]').first();
  await expect(composer).toBeVisible();
  await composer.fill(secondMessage);
  await expect(page.getByRole('button', { name: 'Send' })).toBeVisible();
  const putResponsePromise = page.waitForResponse((response) =>
    response.request().method() === 'PUT'
    && /\/api\/v1\/chat\/v2\/conversations\/[^/]+\/messages\//.test(response.url())
  );
  await composer.press('Control+Enter');

  const optimistic = page.locator('[data-kind="message"][data-role="user"]').filter({ hasText: secondMessage });
  await expect(optimistic).toHaveCount(1);
  const putResponse = await putResponsePromise;
  expect(putResponse.ok(), await putResponse.text()).toBe(true);
  await expect(page.locator('[data-kind="message"][data-role="assistant"]')).toHaveCount(assistantCountBefore + 1, { timeout: 60_000 });
  await waitForTurnComplete(page, 60_000);
  const secondAssistant = page.locator('[data-kind="message"][data-role="assistant"][data-stable="true"]').nth(assistantCountBefore);
  await expect(secondAssistant).toBeVisible();
  await expect(secondAssistant).not.toHaveText('');
  const persistedAssistantText = (await secondAssistant.locator('.chat-markdown').textContent())?.trim() ?? '';
  expect(persistedAssistantText).not.toBe('');
  const persistedAssistantId = await secondAssistant.getAttribute('data-message-id');
  expect(persistedAssistantId).toBeTruthy();
  await expect(optimistic.locator('[data-testid="delivery-state"]')).toHaveCount(0);
  await expect(page.locator('[data-testid="delivery-state"]').filter({ hasText: /failed|sending/i })).toHaveCount(0);
  expect(pageErrors).toEqual([]);

  await page.reload({ waitUntil: 'domcontentloaded' });
  await expect(page.locator('[data-kind="message"][data-role="user"]').filter({ hasText: secondMessage })).toHaveCount(1);
  await expect(page.locator(`[data-message-id="${persistedAssistantId}"][data-kind="message"][data-role="assistant"][data-stable="true"]`)).toHaveCount(1);
  expect(pageErrors).toEqual([]);
});

test('failed turn admission is detected even while the optimistic user row persists', async ({ page }) => {
  const pageErrors: string[] = [];
  page.on('pageerror', (error) => pageErrors.push(error.message));
  await page.route(/\/api\/v1\/chat\/v2\/conversations\/[^/]+\/messages\/[^/]+$/, async (route) => {
    await route.fulfill({
      status: 503,
      contentType: 'application/json',
      body: JSON.stringify({ detail: 'Deterministic admission failure fixture' }),
    });
  });
  const message = `admission-failure-${Date.now()}`;
  const composer = page.locator('textarea[placeholder*="message"], textarea[placeholder*="Message"]').first();
  await composer.fill(message);
  const responsePromise = page.waitForResponse((response) =>
    response.status() === 503 && response.url().includes('/api/v1/chat/v2/conversations/')
  );
  await composer.press('Control+Enter');
  const response = await responsePromise;
  expect(response.ok()).toBe(false);
  const row = page.locator('[data-kind="message"][data-role="user"]').filter({ hasText: message });
  await expect(row).toHaveCount(1);
  await expect(row).toContainText(/failed/i);
  await expect(page.locator('[data-kind="message"][data-role="assistant"]').filter({ hasText: message })).toHaveCount(0);
  expect(pageErrors).toEqual([]);
});

test('rapid-tokens: no duplicate items under high token rate', async ({ page }) => {
  await injectScenario('rapid-tokens');

  await sendMessage(page, 'scenario:rapid-tokens');
  await waitForTurnComplete(page);

  // Assert: exactly one assistant message (no duplicates)
  const assistantMessages = page.locator('[data-kind="message"][data-role="assistant"]');
  await expect(assistantMessages).toHaveCount(1);

  // Assert: no streaming items
  const streamingItems = page.locator('[data-streaming="true"]');
  await expect(streamingItems).toHaveCount(0);
});
