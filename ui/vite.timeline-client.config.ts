import { fileURLToPath, URL } from 'node:url';
import { svelte } from '@sveltejs/vite-plugin-svelte';
import { defineConfig } from 'vite';

// Separate local fixture app: no diagnostic route/module in production exports.
export default defineConfig({
  root: fileURLToPath(new URL('./e2e/fixtures/timeline-client', import.meta.url)),
  plugins: [svelte({ configFile: false })],
  build: {
    rollupOptions: { input: {
      index: fileURLToPath(new URL('./e2e/fixtures/timeline-client/index.html', import.meta.url)),
      history: fileURLToPath(new URL('./e2e/fixtures/timeline-client/history.html', import.meta.url)),
      groups: fileURLToPath(new URL('./e2e/fixtures/timeline-client/groups.html', import.meta.url)),
    } },
  },
  resolve: {
    alias: [
      { find: /^\$app\/(environment|navigation|paths)$/, replacement: fileURLToPath(new URL('./e2e/fixtures/timeline-client/environment.ts', import.meta.url)) },
      { find: '$app/state', replacement: fileURLToPath(new URL('./e2e/fixtures/timeline-client/state.svelte.ts', import.meta.url)) },
      { find: '$env/dynamic/public', replacement: fileURLToPath(new URL('./e2e/fixtures/timeline-client/environment.ts', import.meta.url)) },
      { find: '$lib', replacement: fileURLToPath(new URL('./src/lib', import.meta.url)) },
    ],
  },
  server: { host: '127.0.0.1', fs: { allow: [fileURLToPath(new URL('.', import.meta.url))] } },
});
