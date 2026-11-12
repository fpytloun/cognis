// Isolated WK acceptance input, never a production route/export.
import { build } from 'vite';
import { svelte, vitePreprocess } from '@sveltejs/vite-plugin-svelte';
import { resolve } from 'node:path';
import { readFile, writeFile } from 'node:fs/promises';
import { nativePdfWorker } from './native-pdf-worker.mjs';

if (!process.argv[2]) throw new Error('Supply a dedicated empty output directory');
const outDir = resolve(process.argv[2]);
await build({
  configFile: false,
  define: { __COGNIS_APPLE_BUNDLE__: 'true' },
  worker: { plugins: () => [nativePdfWorker()] },
  resolve: { alias: { '$lib': resolve('src/lib') } },
  plugins: [
    svelte({ configFile: false, preprocess: vitePreprocess() }),
    {
      name: 'html-preview-fixture',
      resolveId(id) {
        if (['$app/environment', '$app/navigation', '$app/paths', '$env/dynamic/public'].includes(id)) return '\0fixture:' + id;
      },
      load(id) {
        if (id === '\0fixture:$app/environment') return 'export const browser = true; export const dev = false;';
        if (id === '\0fixture:$env/dynamic/public') return 'export const env = {};';
        if (id === '\0fixture:$app/paths') return 'export const base = ""; export const assets = "cognisbundle://app";';
        if (id === '\0fixture:$app/navigation') return 'export const beforeNavigate = () => {}; export const goto = () => { throw new Error("Fixture navigation forbidden"); };';
      },
    },
  ],
  build: {
    outDir, emptyOutDir: false, target: 'safari17', minify: false, assetsInlineLimit: Infinity,
    lib: { entry: resolve('scripts/html-preview-fixture.ts'), name: 'HTMLPreviewFixture', formats: ['iife'], fileName: () => 'fixture.js' },
    rollupOptions: { output: { inlineDynamicImports: true } },
  },
});
await writeFile(resolve(outDir, 'index.html'),
  '<!doctype html><html><head></head><body><!-- __sveltekit_test.app.start( -->' +
  '<div id="fixture"></div><script>' +
  (await readFile(resolve(outDir, 'fixture.js'), 'utf8')).replaceAll('</script', '<\\/script') +
  '</script></body></html>');
