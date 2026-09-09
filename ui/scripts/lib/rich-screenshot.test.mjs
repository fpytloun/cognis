import assert from 'node:assert/strict';
import test from 'node:test';

import { assertImageLoaded, waitForChartsReady } from './rich-screenshot.mjs';

test('assertImageLoaded rejects a completed failed image', () => {
  assert.throws(
    () => assertImageLoaded({ complete: true, naturalWidth: 0, src: 'broken.png' }),
    /Rich fixture image did not load: broken\.png/,
  );
});

test('waitForChartsReady rejects after its bounded timeout', async () => {
  let reads = 0;
  await assert.rejects(
    waitForChartsReady(
      async () => {
        reads += 1;
        return ['false'];
      },
      { attempts: 2, intervalMs: 0, sleep: async () => {} },
    ),
    /Rich fixture charts did not become ready: 1 pending/,
  );
  assert.equal(reads, 3);
});

test('waitForChartsReady accepts an empty chart set and a ready chart', async () => {
  await waitForChartsReady(async () => []);
  await waitForChartsReady(async () => ['true']);
});
