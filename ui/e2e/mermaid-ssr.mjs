import assert from 'node:assert/strict';
import { createServer } from 'vite';
import { JSDOM } from 'jsdom';

assert.equal(typeof window, 'undefined');
assert.equal(typeof document, 'undefined');
const server = await createServer({ server: { middlewareMode: true, hmr: false } });
try {
  const { render } = await server.ssrLoadModule('svelte/server');
  // Qualify the actual source-fallback owner independently of the full
  // viewer's existing browser-only DOMPurify integration.
  const { default: Block } = await server.ssrLoadModule('/src/lib/components/rich/blocks/MermaidBlock.svelte');
  const source = 'graph TD; A["<script>unsafe()</script>"] --> B;';
  const result = render(Block, { props: {
    block: { type: 'mermaid', source },
  } });
  assert.match(result.body, /data-mermaid-source/);
  const fragment = JSDOM.fragment(result.body);
  assert.equal(fragment.querySelector('pre[data-mermaid-source]').textContent, source);
  assert.equal(fragment.querySelector('script'), null);
  assert.doesNotMatch(result.body, /<script>unsafe/);
  for (const [id, module] of server.environments.ssr.moduleGraph.idToModuleMap) {
    if (/\/(?:mermaid-presentation|namespace-svg)\.ts$/.test(id) || id.includes('/node_modules/mermaid/')) {
      assert.equal(Boolean(module.ssrModule), false, `SSR evaluated optional diagram presentation: ${id}`);
    }
  }
  console.log('Mermaid SSR: escaped readable fallback; no browser globals or optional presentation evaluated.');
} finally {
  await server.close();
}
