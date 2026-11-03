import { fileURLToPath, URL } from 'node:url';
import { svelte } from '@sveltejs/vite-plugin-svelte';
import { defineConfig } from 'vite';
const shim = fileURLToPath(new URL('./e2e/fixtures/environment.ts', import.meta.url));
export default defineConfig({
  root: fileURLToPath(new URL('./e2e/fixtures/activity-drawer', import.meta.url)),
  plugins: [svelte({ configFile: false })],
  resolve: {
    alias: [
      { find: '$lib/api/client', replacement: fileURLToPath(new URL('./e2e/fixtures/activity-drawer/api.ts', import.meta.url)) },
      { find: '$app/environment', replacement: shim },
      { find: '$app/navigation', replacement: shim },
      { find: '$app/paths', replacement: shim },
      { find: '$env/dynamic/public', replacement: shim },
      { find: '$lib', replacement: fileURLToPath(new URL('./src/lib', import.meta.url)) },
    ],
  },
  server: { fs: { allow: [fileURLToPath(new URL('.', import.meta.url))] } },
});
