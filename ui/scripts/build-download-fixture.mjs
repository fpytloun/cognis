// Compiled production controls/helpers in an isolated test document, never a product route.
import { build } from 'vite';
import { svelte, vitePreprocess } from '@sveltejs/vite-plugin-svelte';
import { resolve } from 'node:path';
import { readFile, writeFile } from 'node:fs/promises';
import { execFileSync } from 'node:child_process';

if (!process.argv[2]) throw new Error('Supply a dedicated output directory');
// A clean npm install has not generated the tsconfig consumed by preprocessing.
execFileSync('npx', ['svelte-kit', 'sync'], { stdio: 'inherit' });
const outDir = resolve(process.argv[2]);
await build({
  configFile: false,
  resolve: { alias: { '$lib': resolve('src/lib') } },
  plugins: [svelte({ configFile: false, preprocess: vitePreprocess() })],
  build: {
    outDir, emptyOutDir: false, target: 'safari17', minify: false,
    lib: { entry: resolve('scripts/download-fixture.ts'), name: 'DownloadFixture', formats: ['iife'], fileName: () => 'fixture.js' },
    rollupOptions: { output: { inlineDynamicImports: true } },
  },
});
await writeFile(resolve(outDir, 'index.html'),
  '<!doctype html><html><head></head><body><!-- __sveltekit_test.app.start( -->' +
  '<div id="formats"></div><div id="attachment"></div><script>' +
  (await readFile(resolve(outDir, 'fixture.js'), 'utf8')).replaceAll('</script', '<\\/script') +
  '</script></body></html>');
