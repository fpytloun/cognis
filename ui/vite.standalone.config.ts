import { fileURLToPath, URL } from 'node:url';
import { svelte } from '@sveltejs/vite-plugin-svelte';
import { defineConfig, type Plugin } from 'vite';
import { standaloneDependencyViolation } from './src/standalone-build-policy';

const standaloneBase = '/api/v1/deliverables/standalone-assets/';

function enforceStandaloneDependencyBoundary(): Plugin {
  return {
    name: 'cognis-standalone-dependency-boundary',
    generateBundle(_options, bundle) {
      const eagerChunks = new Set<string>();
      const collectEagerChunks = (fileName: string): void => {
        if (eagerChunks.has(fileName)) return;
        const output = bundle[fileName];
        if (!output || output.type !== 'chunk') return;
        eagerChunks.add(fileName);
        for (const imported of output.imports) collectEagerChunks(imported);
      };
      for (const output of Object.values(bundle)) {
        if (output.type === 'chunk' && output.isEntry) collectEagerChunks(output.fileName);
      }
      for (const output of Object.values(bundle)) {
        if (output.type !== 'chunk') continue;
        const moduleIds = Object.keys(output.modules);
        const violation = standaloneDependencyViolation(moduleIds, {
          allowLazyHeavy: !eagerChunks.has(output.fileName),
        });
        if (violation) this.error(`Standalone bundle ${violation}`);
      }
    },
  };
}

export default defineConfig({
  base: standaloneBase,
  plugins: [svelte(), enforceStandaloneDependencyBoundary()],
  resolve: {
    alias: {
      $lib: fileURLToPath(new URL('./src/lib', import.meta.url)),
    },
  },
  build: {
    assetsDir: 'assets',
    chunkSizeWarningLimit: 250,
    emptyOutDir: true,
    manifest: true,
    modulePreload: { polyfill: false },
    outDir: 'standalone-build',
    rollupOptions: {
      input: fileURLToPath(new URL('./src/standalone.ts', import.meta.url)),
      output: {
        assetFileNames: 'assets/[name]-[hash][extname]',
        chunkFileNames: 'assets/[name]-[hash].js',
        entryFileNames: 'assets/[name]-[hash].js',
      },
    },
    sourcemap: false,
  },
});
