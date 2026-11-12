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

function excludePrivateFavoriteControls(offline: boolean): Plugin {
  return {
    name: 'cognis-standalone-private-favorites',
    enforce: 'pre',
    resolveId(source) {
      if (source.endsWith('/DocumentFavoriteButton.svelte')) {
        return fileURLToPath(new URL('./src/lib/components/DocumentFavoriteButtonDisabled.svelte', import.meta.url));
      }
      if (offline && /\/(?:PublicFeedbackHost|FeedbackHost)\.svelte$/.test(source)) {
        return fileURLToPath(new URL('./src/lib/components/feedback/FeedbackDisabled.svelte', import.meta.url));
      }
    },
  };
}

export default defineConfig(({ mode }) => ({
  base: mode === 'offline' ? './' : standaloneBase,
  // Offline publications deliberately include lazy renderers (Mermaid,
  // charts, markdown) in one script; the hosted entry retains its budget.
  plugins: mode === 'offline'
    ? [excludePrivateFavoriteControls(true), svelte()]
    : [excludePrivateFavoriteControls(false), svelte(), enforceStandaloneDependencyBoundary()],
  resolve: {
    alias: {
      '$app/paths': fileURLToPath(new URL('./src/standalone-paths.ts', import.meta.url)),
      '$lib/config': fileURLToPath(new URL('./src/standalone-config.ts', import.meta.url)),
      $lib: fileURLToPath(new URL('./src/lib', import.meta.url)),
    },
  },
  build: {
    assetsDir: 'assets',
    chunkSizeWarningLimit: 250,
    emptyOutDir: true,
    manifest: true,
    modulePreload: { polyfill: false },
    outDir: mode === 'offline' ? 'standalone-build/offline' : 'standalone-build',
    assetsInlineLimit: mode === 'offline' ? Number.MAX_SAFE_INTEGER : 4096,
    cssCodeSplit: mode !== 'offline',
    rollupOptions: {
      input: fileURLToPath(new URL('./src/standalone.ts', import.meta.url)),
      output: {
        ...(mode === 'offline' ? { format: 'iife' as const, inlineDynamicImports: true } : {}),
        assetFileNames: 'assets/[name]-[hash][extname]',
        chunkFileNames: 'assets/[name]-[hash].js',
        entryFileNames: 'assets/[name]-[hash].js',
      },
    },
    sourcemap: false,
  },
}));
