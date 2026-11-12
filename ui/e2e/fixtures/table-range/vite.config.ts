import { readFile } from 'node:fs/promises';
import { defineConfig, mergeConfig, type Plugin } from 'vite';
import appConfig from '../../../vite.config';

const fixture: Plugin = {
  name: 'table-range-fixture',
  configureServer(server) {
    server.middlewares.use(async (request, response, next) => {
      const entry = '/e2e/fixtures/table-range/index.html';
      if (request.url?.split('?')[0] !== entry) return next();
      try {
        const html = await server.transformIndexHtml(entry, await readFile(new URL('./index.html', import.meta.url), 'utf8'));
        response.setHeader('Content-Type', 'text/html');
        response.end(html);
      } catch (error) { next(error); }
    });
  },
};

export default defineConfig(mergeConfig(appConfig, { plugins: [fixture], resolve: {
  alias: { '$env/dynamic/public': new URL('../resource-revisions/public-env.ts', import.meta.url).pathname },
} }));
