import { mkdir, writeFile } from 'node:fs/promises';
import { resolve } from 'node:path';
import { test, expect } from '@playwright/test';
import { injectScenario, login, openOrCreateConversation, sendMessage } from './helpers';

test('creates a real dashboard source for feedback acceptance', async ({ page }, testInfo) => {
  test.setTimeout(90_000);
  await login(page);
  await injectScenario('work-feedback-dashboard');
  await openOrCreateConversation(page);

  const documentResponse = page.waitForResponse((response) => {
    const url = new URL(response.url());
    return response.request().method() === 'GET'
      && /^\/api\/v1\/deliverables\/dlv_[a-f0-9]+$/.test(url.pathname)
      && response.ok();
  }, { timeout: 60_000 });
  await sendMessage(page, 'scenario:work-feedback-dashboard');
  const document = await (await documentResponse).json();
  expect(document.title).toBe('Daily actions dashboard');
  expect(document.format).toBe('rich');
  await expect(page.getByText('Synthetic content for Work Feedback acceptance.', {
    exact: false
  }).first()).toBeVisible();

  const conversationId = new URL(page.url()).pathname.split('/').at(-1);
  const sources = {
    conversation_id: conversationId,
    deliverable_id: document.deliverable_id,
    revision: document.revision,
    lineage_id: document.lineage_id,
    source_url: page.url(),
    synthetic: true
  };
  const directory = resolve(testInfo.config.rootDir, '../review-artifacts/work-feedback');
  await mkdir(directory, { recursive: true });
  await writeFile(resolve(directory, 'dashboard-source.json'), JSON.stringify(sources, null, 2));
  await testInfo.attach('dashboard-source', {
    body: JSON.stringify(sources, null, 2),
    contentType: 'application/json'
  });
});
