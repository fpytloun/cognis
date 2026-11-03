import { expect, test as base } from '@playwright/test';

const test = base.extend({
  context: async ({ playwright, browserName, contextOptions, launchOptions, baseURL, headless }, use, testInfo) => {
    // Match the companion's persistent, profile-scoped WKWebsiteDataStore.
    const context = await playwright[browserName].launchPersistentContext(
      testInfo.outputPath('native-storage-profile'),
      { ...contextOptions, baseURL, headless, ...launchOptions },
    );
    try {
      await use(context);
    } finally {
      await context.close();
    }
  },
});

test('native structured clone, callback abort, bytes and lease boundary', async ({ page }, testInfo) => {
  await page.route(url => url.pathname.startsWith('/api/'), route => route.abort());
  await page.goto('/');
  await page.getByRole('button', { name: 'Run tiny PNG/JPEG diagnostic' }).click();
  await expect(page.locator('#result')).toContainText('"negative"');
  const result = JSON.parse((await page.locator('#result').textContent())!);
  await testInfo.attach('native-idb-evidence', { body: JSON.stringify(result, null, 2), contentType: 'application/json' });
  expect(result.failure).toBeUndefined();
  expect(result.negative.name).toBe('AbortError');
  expect(result.bytes).toHaveLength(4);
  for (const bytes of result.bytes) expect(bytes).toMatchObject({ committed: true, equal: true });
  expect(result.metadata).toBe(true);
  expect(result.revoked).toBe(true);
  for (const key of ['sameReservation', 'sameActivation', 'directBlocked', 'preparingNotDrained',
    'consumed', 'sameRetry', 'receiptStored', 'receiptUnchanged', 'stagingRetained', 'staleRetained',
    'revoked', 'oneSend', 'oneUpload', 'modeSemantics']) expect(result.protocol?.[key], key).toBe(true);
  await page.reload();
  await page.evaluate(() => (window as unknown as { verifyReload: () => void }).verifyReload());
  await expect(page.locator('#result')).toContainText('"reloadBinding"');
  const reloaded = JSON.parse((await page.locator('#result').textContent())!);
  expect(reloaded.failure).toBeUndefined();
  for (const key of ['reloadBinding', 'reloadReceipt', 'reloadUploadReceipt', 'reloadMetadata',
    'reloadPNG', 'reloadJPEG', 'reloadAllFourBytes']) expect(reloaded[key], key).toBe(true);
});
