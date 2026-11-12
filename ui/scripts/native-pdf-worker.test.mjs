import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { nativePdfWorker } from './native-pdf-worker.mjs';

test('compiles only the installed PDF worker decoder import and fails closed on upstream changes', async () => {
  const plugin = nativePdfWorker();
  const path = '/node_modules/pdfjs-dist/legacy/build/pdf.worker.mjs';
  const source = await readFile(new URL('../node_modules/pdfjs-dist/legacy/build/pdf.worker.mjs', import.meta.url), 'utf8');
  const result = plugin.transform(source, path);
  assert.ok(result.code.includes('import openjpegFallback from'));
  assert.ok(result.code.includes('import jbig2Fallback from'));
  assert.ok(!result.code.includes('`${WasmImage.#wasmUrl}${this._noWasmFilename}`'));
  assert.equal(plugin.transform(source, '/unrelated.mjs'), undefined);
  assert.throws(() => plugin.transform('', path), /revalidate native worker packaging/);
});
