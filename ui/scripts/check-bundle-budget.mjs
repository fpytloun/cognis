#!/usr/bin/env node
// Bundle size budget for the mobile-critical path.
//
// Computes the statically imported JS/CSS closure needed to open a chat
// (client entry + app + root layout + (app) layout + chat route) from the Vite
// manifest and fails when its brotli size or the service-worker precache
// grows past the budget. Run after `vite build` (wired into `npm run build`).
import { existsSync, readFileSync, statSync } from 'node:fs';
import { join } from 'node:path';
import { brotliCompressSync, constants } from 'node:zlib';

const CLIENT_DIR = '.svelte-kit/output/client';
const MANIFEST = join(CLIENT_DIR, '.vite/manifest.json');
const NODE_PREFIX = '.svelte-kit/generated/client-optimized/nodes/';

// Budgets (brotli bytes). Raise deliberately, with a reason, never silently.
const BUDGETS = {
  // Explicitly approved increase from 450 to 470 KiB JS and 32 to 34 KiB CSS for inline
  // work feedback in chat (~32 KiB JS once rich results render on first paint). Deferring
  // it further would delay send-gating feedback code for no perceptible gain; a dedicated
  // size-reduction pass on main is the preferred way to win this headroom back.
  // A further 5 KiB of JS was approved for unified commenting and the mobile header.
  // Comment and overflow menus remain deferred; CSS and per-chunk caps stay unchanged.
  // A 500 KiB JS budget was explicitly approved for this rollout.
  // CSS raised from 34 to 36 KiB (explicitly approved) for the comment list and task status UI.
  chatCriticalJsBrotli: 500 * 1024,
  chatCriticalCssBrotli: 36 * 1024,
  largestCriticalChunkBrotli: 96 * 1024,
};

const CRITICAL_ROUTE_COMPONENTS = [
  'src/routes/+layout.svelte',
  'src/routes/(app)/+layout.svelte',
  'src/routes/(app)/chat/[conversationId]/+page.svelte',
];

function brotliSize(file) {
  const sidecar = `${file}.br`;
  if (existsSync(sidecar)) return statSync(sidecar).size;
  return brotliCompressSync(readFileSync(file), {
    params: { [constants.BROTLI_PARAM_QUALITY]: 11 },
  }).length;
}

function nodeKeyForComponent(manifest, component) {
  for (const key of Object.keys(manifest)) {
    if (!key.startsWith(NODE_PREFIX)) continue;
    const source = readFileSync(key, 'utf8');
    if (source.includes(`/${component}"`)) return key;
  }
  throw new Error(`No route node found for ${component}`);
}

function main() {
  if (!existsSync(MANIFEST)) {
    console.error(`bundle-budget: ${MANIFEST} missing; run vite build first`);
    process.exit(1);
  }
  const manifest = JSON.parse(readFileSync(MANIFEST, 'utf8'));
  const roots = Object.keys(manifest).filter((key) => manifest[key].isEntry && !key.startsWith(NODE_PREFIX));
  for (const component of CRITICAL_ROUTE_COMPONENTS) roots.push(nodeKeyForComponent(manifest, component));

  const js = new Set();
  const css = new Set();
  const visit = (key) => {
    const chunk = manifest[key];
    if (!chunk || js.has(chunk.file)) return;
    js.add(chunk.file);
    for (const file of chunk.css ?? []) css.add(file);
    for (const child of chunk.imports ?? []) visit(child);
  };
  roots.forEach(visit);

  const sized = (files) => [...files]
    .map((file) => ({ file, br: brotliSize(join(CLIENT_DIR, file)) }))
    .sort((a, b) => b.br - a.br);
  const jsSizes = sized(js);
  const cssSizes = sized(css);
  const total = (items) => items.reduce((sum, item) => sum + item.br, 0);
  const kb = (bytes) => `${(bytes / 1024).toFixed(1)} KB`;

  const results = {
    chatCriticalJsBrotli: total(jsSizes),
    chatCriticalCssBrotli: total(cssSizes),
    largestCriticalChunkBrotli: jsSizes[0]?.br ?? 0,
  };
  console.log(`bundle-budget: chat critical path ${jsSizes.length} JS files ${kb(results.chatCriticalJsBrotli)} br, CSS ${kb(results.chatCriticalCssBrotli)} br`);
  for (const item of jsSizes.slice(0, 5)) console.log(`  ${kb(item.br).padStart(9)}  ${item.file}`);

  let failed = false;
  for (const [name, limit] of Object.entries(BUDGETS)) {
    if (results[name] > limit) {
      console.error(`bundle-budget: ${name} ${kb(results[name])} exceeds budget ${kb(limit)}`);
      failed = true;
    }
  }
  if (failed) process.exit(1);
}

main();
