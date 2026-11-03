import { expect, test, type Page, type TestInfo } from '@playwright/test';

import {
  clearScenario,
  injectScenario,
  login,
  openOrCreateConversation,
  sendMessage,
  waitForTurnComplete,
} from './helpers';

/**
 * Deliverable revisions through the real stack: the mock LLM writes a
 * deliverable, then revises it with write_deliverable(supersedes=...).
 *
 * Prerequisites: make e2e-up && make e2e-seed
 */

async function capture(page: Page, testInfo: TestInfo, name: string): Promise<void> {
  const path = testInfo.outputPath(name);
  await page.screenshot({ path, fullPage: false });
  await testInfo.attach(name, { path, contentType: 'image/png' });
}

test.beforeEach(async ({ page }) => {
  await login(page);
  await openOrCreateConversation(page);
});

test.afterEach(async () => {
  await clearScenario().catch(() => {});
});

test('deliverable revisions: chat shows the latest revision and browses history', async ({ page }, testInfo) => {
  test.setTimeout(180_000);
  await page.setViewportSize({ width: 1440, height: 1000 });
  await injectScenario('deliverable-revisions');

  await sendMessage(page, 'scenario:deliverable-revisions draft the launch plan');
  await waitForTurnComplete(page, 90_000);
  // Timeline cards only; the inspector renders its own deliverable cards.
  const cards = page.locator(
    "xpath=//div[contains(concat(' ', @class, ' '), ' assistant-deliverable-wrapper ')]"
      + "[not(ancestor::*[@id='conversation-info-drawer'])]",
  );
  await expect(cards).toHaveCount(1);
  await expect(cards.first().getByTestId('deliverable-revision-bar')).toHaveCount(0);

  await sendMessage(page, 'Please tighten the plan to 20 partners and add risks.');
  await waitForTurnComplete(page, 90_000);
  // The new card appears when the turn's final response is presented.
  await expect(cards).toHaveCount(2, { timeout: 30_000 });

  const original = cards.nth(0);
  const revised = cards.nth(1);
  // The earlier card stays in the chat history but is marked as superseded.
  await expect(original.getByTestId('deliverable-superseded-notice')).toHaveText('Superseded by revision 2');
  await expect(original.getByTestId('deliverable-revision-toggle')).toHaveText(/Revision 1 of 2/);
  await expect(revised.getByTestId('deliverable-revision-toggle')).toHaveText(/Revision 2 of 2/);
  await expect(revised.getByTestId('deliverable-superseded-notice')).toHaveCount(0);
  await revised.scrollIntoViewIfNeeded();
  await capture(page, testInfo, '01-chat-latest-and-superseded.png');

  await revised.getByTestId('deliverable-revision-toggle').click();
  const options = revised.getByTestId('deliverable-revision-option');
  await expect(options).toHaveCount(2);
  await expect(options.nth(0)).toContainText('Revision 2');
  await expect(options.nth(0)).toContainText('Latest');
  await capture(page, testInfo, '02-revision-history-open.png');

  await options.nth(1).click();
  await expect(revised.getByTestId('deliverable-revision-toggle')).toHaveText(/Revision 1 of 2/);
  await expect(revised.getByTestId('deliverable-superseded-notice')).toBeVisible();
  await expect(revised).toContainText('Ship the beta to 50 design partners');
  await capture(page, testInfo, '03-older-revision-in-place.png');

  await revised.getByTestId('deliverable-view-latest').click();
  await expect(revised.getByTestId('deliverable-revision-toggle')).toHaveText(/Revision 2 of 2/);
  await expect(revised).toContainText('Ship the beta to 20 design partners');

  // The activity inspector lists one deliverable: the latest revision.
  const inspectorToggle = page.getByRole('button', { name: 'Toggle session details', exact: true });
  await expect(inspectorToggle).toBeVisible();
  await inspectorToggle.click();
  const inspector = page.getByTestId('conversation-info-drawer');
  await expect(inspector).toBeVisible();
  const recent = inspector.getByTestId('recent-work-activity');
  await expect(recent).toBeVisible({ timeout: 30_000 });
  const recentCards = recent.locator('.assistant-deliverable-wrapper');
  await expect(recentCards).toHaveCount(1);
  await expect(recentCards.first().getByTestId('deliverable-revision-toggle')).toHaveText(/Revision 2 of 2/);
  await recentCards.first().scrollIntoViewIfNeeded();
  await capture(page, testInfo, '04-activity-sidebar-latest-only.png');
});
