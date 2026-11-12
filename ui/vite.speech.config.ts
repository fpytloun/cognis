import { defineConfig, mergeConfig } from 'vite';
import { fileURLToPath } from 'node:url';
import base from './vite.config';

/** Serve real components without starting the application or a paid backend. */
export default mergeConfig(base, defineConfig({
  server: { fs: { allow: [fileURLToPath(new URL('./e2e/fixtures', import.meta.url))] } },
  resolve: { alias: {
    '$env/dynamic/public': fileURLToPath(new URL('./e2e/fixtures/speech-public-env.ts', import.meta.url)),
  } },
  plugins: [{
    name: 'speech-browser-fixture',
    configureServer(server) {
      server.middlewares.use((request, response, next) => {
        if (request.url?.split('?')[0] !== '/speech-playback-browser-fixture') return next();
        response.setHeader('Content-Type', 'text/html');
        response.end('<!doctype html><html><head><meta name="viewport" content="width=device-width, initial-scale=1"></head><body><div id="fixture"></div><script type="module" src="/e2e/fixtures/speech-main.ts"></script></body></html>');
      });
    },
  }],
}));
