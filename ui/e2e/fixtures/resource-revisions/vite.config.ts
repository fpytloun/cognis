import { readFile } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import { defineConfig, mergeConfig, type Plugin } from 'vite';
import appConfig from '../../../vite.config';

// SvelteKit owns HTML routing; expose just this component fixture before its router.
const fixturePage: Plugin = {
  name: 'resource-revision-fixture',
  configureServer(server) {
    server.middlewares.use(async (request, response, next) => {
      const entry = '/e2e/fixtures/resource-revisions/index.html';
      if (request.url?.split('?')[0] !== entry) return next();
      try {
        const source = await readFile(new URL('./index.html', import.meta.url), 'utf8');
        const html = await server.transformIndexHtml(request.url, source);
        response.statusCode = 200;
        response.setHeader('Content-Type', 'text/html');
        response.end(html);
      } catch (error) {
        next(error);
      }
    });
  },
};

export default defineConfig(mergeConfig(appConfig, {
  plugins: [fixturePage],
  resolve: {
    alias: [{
      find: '$env/dynamic/public',
      replacement: fileURLToPath(new URL('./public-env.ts', import.meta.url)),
    }],
  },
}));
