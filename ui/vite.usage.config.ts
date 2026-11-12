import { fileURLToPath, URL } from 'node:url';
import { svelte } from '@sveltejs/vite-plugin-svelte';
import { defineConfig } from 'vite';

// Framework plumbing only; the real adapter/controller and ApiError remain loaded.
const environment: Record<string, string> = {
  '$env/dynamic/public': 'export const env = {};',
  '$app/environment': 'export const browser = true; export const dev = true; export const building = false; export const version = "usage-protocol-fixture";',
};
export default defineConfig({
  root: 'e2e/usage',
  publicDir: '../../static',
  plugins: [{
    name: 'usage-fixture-framework-environment',
    resolveId(id) { if (Object.hasOwn(environment, id)) return `\0${id}`; },
    load(id) { return id.startsWith('\0') ? environment[id.slice(1)] : undefined; },
  }, svelte()],
  resolve: { alias: { $lib: fileURLToPath(new URL('./src/lib', import.meta.url)) } },
  // The app's PostCSS/Tailwind config lives at the UI root, not the harness root.
  css: { postcss: fileURLToPath(new URL('.', import.meta.url)) },
  cacheDir: fileURLToPath(new URL('./node_modules/.vite-usage', import.meta.url)),
  server: { host: '127.0.0.1', port: 5197, strictPort: true, fs: { allow: [fileURLToPath(new URL('.', import.meta.url))] } },
  build: { outDir: fileURLToPath(new URL('./node_modules/.usage-build', import.meta.url)), emptyOutDir: true }
});
