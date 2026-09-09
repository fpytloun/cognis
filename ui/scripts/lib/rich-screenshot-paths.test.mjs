import assert from 'node:assert/strict';
import path from 'node:path';
import test from 'node:test';

import { defaultRichScreenshotPath } from './rich-screenshot-paths.mjs';

test('defaultRichScreenshotPath uses the supplied platform temporary directory', () => {
  const temporaryDirectory = path.join(path.parse(process.cwd()).root, 'platform-temp');
  assert.equal(
    defaultRichScreenshotPath({
      id: 'research-answer',
      theme: 'dark',
      width: 768,
      temporaryDirectory,
    }),
    path.join(temporaryDirectory, 'rich-scenario-gallery', 'research-answer--dark--768.png'),
  );
});
