import { sveltekit } from '@sveltejs/kit/vite';
import { defineConfig } from 'vite';

export default defineConfig({
  // Production chunks are precompiled; keep cold browser imports independent of dev transforms.
  server: { warmup: { clientFiles: ['./src/lib/components/AssistantDeliverableBlock.svelte'] } },
  plugins: [
    sveltekit(),
    {
      name: 'deliverable-ui-fixture',
      configureServer(server) {
        server.middlewares.use(async (request, response, next) => {
          if (request.url?.startsWith('/api/')) {
            // No live service proxy: optional account/audio reads stay anonymous.
            response.writeHead(401, { 'Content-Type': 'application/json' });
            response.end('{"detail":"Anonymous browser fixture"}');
            return;
          }
          if (!request.url?.startsWith('/__deliverable-fixture')) return next();
          response.setHeader('Content-Type', 'text/html');
          response.end(await server.transformIndexHtml(request.url,
            '<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1"></head><body><div id="fixture"></div><script>globalThis.__sveltekit_dev={env:{}};</script><script type="module" src="/e2e/fixtures/deliverable-ui/main.ts"></script></body></html>'));
        });
      },
    },
  ],
});
