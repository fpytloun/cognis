import { fileURLToPath, URL } from 'node:url';
import { svelte } from '@sveltejs/vite-plugin-svelte';
import { defineConfig } from 'vite';

const shim = fileURLToPath(new URL('./e2e/fixtures/environment.ts', import.meta.url));
// Compile-time probes exist only in this opt-in fixture bundle. Never rewrite
// application sources or silently instrument a normal performance measurement.
const probe = (kind: string, scope: string, controller?: string) =>
  `(globalThis as typeof globalThis & { __dragLifecycle?: (kind: string, scope: { key: string }, controller?: object) => void }).__dragLifecycle?.('${kind}', ${scope}${controller ? `, ${controller}` : ''});`;
function injectOnce(code: string, anchor: string, replacement: string): string {
  if (code.split(anchor).length !== 2) throw new Error(`Drag diagnostic probe anchor changed: ${anchor}`);
  return code.replace(anchor, replacement);
}
export default defineConfig({
  root: 'e2e/window-drag',
  plugins: [
    {
      name: 'window-drag-acquisition-diagnostic',
      enforce: 'pre',
      transform(code, id) {
        if (process.env.DRAG_DIAGNOSTIC !== '1') return;
        const file = id.split('?')[0];
        if (file.endsWith('/chat-v2/ScopedChatV2Timeline.svelte')) {
          code = injectOnce(code, '    const currentScope = { ...scope };\n    let acquired: ReturnType<typeof acquireTimeline>;',
            `    ${probe('view-acquire-effect', 'scope')}\n    const currentScope = { ...scope };\n    let acquired: ReturnType<typeof acquireTimeline>;`);
          code = injectOnce(code, '      acquired.release();',
            `      ${probe('view-detach', 'currentScope', 'acquired.controller')}\n      acquired.release();`);
        } else if (file.endsWith('/chat-v2/timeline-controller.svelte.ts')) {
          code = injectOnce(code, '    let store!: ChatV2Store;',
            `    ${probe('controller-create', 'scope', 'this')}\n    let store!: ChatV2Store;`);
          code = injectOnce(code, '  retain(): void { this.references += 1; }',
            `  retain(): void { ${probe('controller-retain', 'this.scope', 'this')} this.references += 1; }`);
          code = injectOnce(code, '  release(): void {',
            `  release(): void {\n    ${probe('controller-release', 'this.scope', 'this')}`);
          code = injectOnce(code, '  invalidate(): void {',
            `  invalidate(): void {\n    ${probe('controller-invalidate', 'this.scope', 'this')}`);
          code = injectOnce(code, '    this.phase = \'loading\';',
            `    ${probe('controller-load', 'this.scope', 'this')}\n    this.phase = 'loading';`);
          code = injectOnce(code, '      await this.hydrate();',
            `      ${probe('hydrate-start', 'this.scope', 'this')}\n      await this.hydrate();\n      ${probe('hydrate-finished', 'this.scope', 'this')}`);
          code = injectOnce(code, '      const snapshot = await this.api.snapshot(this.scope, { signal, timeoutMs: 15_000 });',
            `      ${probe('snapshot-dispatch', 'this.scope', 'this')}\n      const snapshot = await this.api.snapshot(this.scope, { signal, timeoutMs: 15_000 });\n      ${probe('snapshot-received', 'this.scope', 'this')}`);
          code = injectOnce(code, '      await yieldBeforeProjection();',
            `      await yieldBeforeProjection();\n      ${probe('projection-yielded', 'this.scope', 'this')}`);
          code = injectOnce(code, 'export function clearTimelineMemory(): void {',
            `export function clearTimelineMemory(): void {\n  (globalThis as typeof globalThis & { __dragOwnerClear?: () => void }).__dragOwnerClear?.();`);
          const failure = "this.phase = 'error';";
          if (code.split(failure).length !== 3) throw new Error('Drag diagnostic error probe anchors changed');
          code = code.replaceAll(failure, `${probe('controller-error', 'this.scope', 'this')} ${failure}`);
          code = injectOnce(code, '  controller.retain();',
            `  ${probe('canonical-acquire', 'scope', 'controller')}\n  controller.retain();`);
          const ready = "this.phase = 'ready';";
          if (code.split(ready).length !== 4) throw new Error('Drag diagnostic ready probe anchors changed');
          code = code.replaceAll(ready, `${probe('controller-ready', 'this.scope', 'this')} ${ready}`);
        } else return;
        return { code, map: null };
      },
    },
    svelte(),
  ],
  resolve: {
    alias: {
      $lib: fileURLToPath(new URL('./src/lib', import.meta.url)),
      '$app/environment': shim,
      '$app/navigation': shim,
      '$app/paths': shim,
      '$env/dynamic/public': shim,
      '$env/static/public': shim,
    },
  },
  build: { outDir: '../../.window-drag-build', emptyOutDir: true, minify: false, sourcemap: true },
});
