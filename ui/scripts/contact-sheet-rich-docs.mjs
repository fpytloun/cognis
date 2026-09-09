import { mkdir, readFile } from 'node:fs/promises';
import path from 'node:path';
import { createContactSheet } from './lib/contact-sheet.mjs';

const assetDir = path.resolve(process.argv[2] ?? '../docs/assets/screenshots/rich-deliverables');
const output = path.resolve(process.argv[3] ?? 'review-artifacts/rich-scenario-gallery/foundation/contact-sheets/docs-previews.png');
const manifest = JSON.parse(await readFile(path.join(assetDir, 'manifest.json'), 'utf8'));
await mkdir(path.dirname(output), { recursive: true });
await createContactSheet(
  manifest.previews.map((preview) => ({
    path: path.join(assetDir, preview.filename),
    label: preview.id,
  })),
  output,
  { columns: 4, cellWidth: 360 },
);
console.log(output);
