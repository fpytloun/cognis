import { gzipSync } from 'node:zlib';
import { readFile } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const entryKey = 'src/standalone.ts';
export const standaloneGzipBudgetBytes = 180 * 1024;

export async function checkStandaloneBuild({
  buildDir = path.resolve('standalone-build'),
  gzipBudgetBytes = standaloneGzipBudgetBytes,
} = {}) {
  const manifestPath = path.join(buildDir, '.vite', 'manifest.json');
  const manifest = JSON.parse(await readFile(manifestPath, 'utf8'));
  const entry = manifest[entryKey];
  if (!entry?.isEntry || typeof entry.file !== 'string') {
    throw new Error(`Standalone manifest is missing entry ${entryKey}`);
  }

  const initialFiles = new Set();
  function collectInitial(key) {
    const item = manifest[key];
    if (!item) throw new Error(`Standalone manifest references missing import ${key}`);
    initialFiles.add(item.file);
    for (const css of item.css ?? []) initialFiles.add(css);
    for (const imported of item.imports ?? []) collectInitial(imported);
  }
  collectInitial(entryKey);

  let gzipBytes = 0;
  for (const relativePath of initialFiles) {
    const content = await readFile(path.join(buildDir, relativePath));
    gzipBytes += gzipSync(content, { level: 9 }).byteLength;
  }
  if (gzipBytes > gzipBudgetBytes) {
    throw new Error(
      `Standalone initial bundle is ${gzipBytes} gzip bytes; budget is ${gzipBudgetBytes} bytes`,
    );
  }
  return { fileCount: initialFiles.size, gzipBytes, gzipBudgetBytes };
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  const result = await checkStandaloneBuild();
  console.log(
    `Standalone initial bundle: ${result.gzipBytes} gzip bytes `
    + `(${result.fileCount} files, budget ${result.gzipBudgetBytes})`,
  );
}
