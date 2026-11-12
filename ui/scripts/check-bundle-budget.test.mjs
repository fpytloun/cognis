import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { mkdtempSync, mkdirSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import test from 'node:test';

const checker = fileURLToPath(new URL('./check-bundle-budget.mjs', import.meta.url));
const nodePrefix = '.svelte-kit/generated/client-optimized/nodes/';
const components = [
  'src/routes/+layout.svelte',
  'src/routes/(app)/+layout.svelte',
  'src/routes/(app)/chat/[conversationId]/+page.svelte',
];

function runFixture(t, { sizes = Array(5).fill(97280), css = 0, child = 0, dynamic = 0, sidecars = true } = {}) {
  const cwd = mkdtempSync(join(tmpdir(), 'cognis-bundle-budget-test-'));
  t.after(() => rmSync(cwd, { recursive: true, force: true }));
  const client = join(cwd, '.svelte-kit/output/client');
  const manifest = {};
  function asset(file, size) {
    const path = join(client, file);
    mkdirSync(dirname(path), { recursive: true });
    writeFileSync(path, sidecars ? 'export {};' : 'a'.repeat(size));
    if (sidecars) writeFileSync(`${path}.br`, Buffer.alloc(size));
  }
  sizes.forEach((size, index) => {
    const key = index < 2 ? `entry-${index}` : `${nodePrefix}${index}.js`;
    manifest[key] = { file: `chunk-${index}.js`, ...(index < 2 ? { isEntry: true } : {}) };
    asset(manifest[key].file, size);
    if (index >= 2) {
      const path = join(cwd, key);
      mkdirSync(dirname(path), { recursive: true });
      writeFileSync(path, `export { default } from "/${components[index - 2]}"`);
    }
  });
  if (css) {
    manifest['entry-0'].css = ['style.css'];
    asset('style.css', css);
  }
  for (const [key, size, field] of [
    ['child', child, 'imports'],
    ['dynamic', dynamic, 'dynamicImports'],
  ]) {
    if (!size) continue;
    manifest[key] = { file: `${key}.js` };
    manifest['entry-0'][field] = [key];
    asset(`${key}.js`, size);
  }
  mkdirSync(join(client, '.vite'), { recursive: true });
  writeFileSync(join(client, '.vite/manifest.json'), JSON.stringify(manifest));
  return spawnSync(process.execPath, [checker], { cwd, encoding: 'utf8' });
}

test('accepts exactly 512000 bytes of critical JavaScript', (t) => {
  const result = runFixture(t, { child: 25600 });
  assert.equal(result.status, 0, result.stderr);
  assert.match(result.stdout, /6 JS files 500\.0 KB br/);
});

test('rejects one byte above 512000 through a static import', (t) => {
  const result = runFixture(t, { child: 25601 });
  assert.equal(result.status, 1);
  assert.match(result.stderr, /chatCriticalJsBrotli .* exceeds budget 500\.0 KB/);
});

test('does not count dynamic imports in the static critical closure', (t) => {
  const result = runFixture(t, { dynamic: 100000 });
  assert.equal(result.status, 0, result.stderr);
});

test('accepts the CSS and unchanged largest-chunk boundaries', (t) => {
  const result = runFixture(t, { sizes: [98304, 1, 1, 1, 1], css: 36864 });
  assert.equal(result.status, 0, result.stderr);
});

test('rejects one byte above the 36 KiB CSS budget', (t) => {
  const result = runFixture(t, { sizes: [1, 1, 1, 1, 1], css: 36865 });
  assert.equal(result.status, 1);
  assert.match(result.stderr, /chatCriticalCssBrotli .* exceeds budget 36\.0 KB/);
});

test('rejects one byte above the unchanged 96 KiB chunk budget', (t) => {
  const result = runFixture(t, { sizes: [98305, 1, 1, 1, 1] });
  assert.equal(result.status, 1);
  assert.match(result.stderr, /largestCriticalChunkBrotli .* exceeds budget 96\.0 KB/);
});

test('compresses assets when Brotli sidecars are absent', (t) => {
  const result = runFixture(t, { sizes: Array(5).fill(100000), sidecars: false });
  assert.equal(result.status, 0, result.stderr);
});
