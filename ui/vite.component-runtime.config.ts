import { fileURLToPath, URL } from 'node:url';
import { svelte } from '@sveltejs/vite-plugin-svelte';
import { defineConfig } from 'vite';

export default defineConfig({
  root: fileURLToPath(new URL('./e2e/fixtures/component-runtime', import.meta.url)),
  publicDir: fileURLToPath(new URL('./static', import.meta.url)),
  plugins: [svelte({ configFile: false })],
  build: {
    outDir: fileURLToPath(new URL('./.component-runtime-build', import.meta.url)),
    emptyOutDir: true,
    rollupOptions: { input: {
      brand: fileURLToPath(new URL('./e2e/fixtures/component-runtime/brand.html', import.meta.url)),
      streaming: fileURLToPath(new URL('./e2e/fixtures/component-runtime/streaming.html', import.meta.url)),
      scoped: fileURLToPath(new URL('./e2e/fixtures/component-runtime/scoped.html', import.meta.url)),
    } },
  },
  resolve: {
    alias: [
      // This isolated fixture compiles the existing dev-only route, never the app server.
      { find: /^\$app\/(environment|navigation|paths)$/, replacement: fileURLToPath(new URL('./e2e/fixtures/timeline-client/environment.ts', import.meta.url)) },
      { find: '$app/state', replacement: fileURLToPath(new URL('./e2e/fixtures/timeline-client/state.svelte.ts', import.meta.url)) },
      { find: '$env/dynamic/public', replacement: fileURLToPath(new URL('./e2e/fixtures/timeline-client/environment.ts', import.meta.url)) },
      { find: '$lib', replacement: fileURLToPath(new URL('./src/lib', import.meta.url)) },
    ],
  },
  server: { host: '127.0.0.1', fs: { allow: [fileURLToPath(new URL('.', import.meta.url))] } },
});
