import os from 'node:os';
import path from 'node:path';

export function defaultRichScreenshotPath({ id, theme, width, temporaryDirectory = os.tmpdir() }) {
  return path.join(temporaryDirectory, 'rich-scenario-gallery', `${id}--${theme}--${width}.png`);
}
