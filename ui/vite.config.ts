import { availableParallelism } from 'node:os';
import { fileURLToPath } from 'node:url';

import { sveltekit } from '@sveltejs/kit/vite';
import { svelteTesting } from '@testing-library/svelte/vite';
import { defineConfig } from 'vitest/config';
import { viteStaticCopy } from 'vite-plugin-static-copy';
import { nativePdfWorker } from './scripts/native-pdf-worker.mjs';

export default defineConfig({
  define: { __COGNIS_APPLE_BUNDLE__: JSON.stringify(process.env.COGNIS_APPLE_BUNDLE === '1') },
  worker: { plugins: () => process.env.COGNIS_APPLE_BUNDLE === '1' ? [nativePdfWorker()] : [] },
  plugins: [sveltekit(), svelteTesting(), viteStaticCopy({
    targets: [
      { src: 'node_modules/pdfjs-dist/cmaps/*', dest: 'pdfjs/cmaps', rename: { stripBase: true } },
      { src: 'node_modules/pdfjs-dist/standard_fonts/*', dest: 'pdfjs/standard_fonts', rename: { stripBase: true } },
      { src: 'node_modules/pdfjs-dist/wasm/*', dest: 'pdfjs/wasm', rename: { stripBase: true } },
    ],
  })],
  server: {
    fs: {
      allow: [fileURLToPath(new URL('../docs', import.meta.url))]
    },
    proxy: {
      '/api': {
        target: 'http://localhost:8080',
        changeOrigin: true,
        ws: true
      },
      '/.well-known': {
        target: 'http://localhost:8080',
        changeOrigin: true
      }
    }
  },
  build: {
    ...(process.env.COGNIS_APPLE_BUNDLE === '1' ? { assetsInlineLimit: Infinity } : {}),
    // Warn when a chunk exceeds 250KB (uncompressed). Initial-load chunks
    // should stay well below this to keep mobile time-to-interactive low.
    chunkSizeWarningLimit: 250
  },
  test: {
    maxWorkers: Math.max(1, Math.floor(availableParallelism() / 2)),
    environment: 'jsdom',
    include: ['src/**/*.{test,spec}.{ts,js}'],
    setupFiles: ['./src/test/setup.ts'],
    // Local-time rendering is deterministic across hosts and CI (DST-observing zone).
    env: { TZ: 'Europe/Prague' }
  }
});
