// Build only synthetic test inputs. Does not install or modify the user's app.
import { build } from 'vite';
import { svelte, vitePreprocess } from '@sveltejs/vite-plugin-svelte';
import { resolve } from 'node:path';
import { cp, mkdir, readFile, writeFile } from 'node:fs/promises';
import { execFileSync } from 'node:child_process';
import { nativePdfWorker } from './native-pdf-worker.mjs';

if (!process.argv[2]) throw new Error('Supply a dedicated fixture output directory');
const outDir = resolve(process.argv[2]);
const native = !process.argv.includes('--web');
await mkdir(outDir, { recursive: true });
execFileSync('ffmpeg', ['-y', '-f', 'lavfi', '-i', 'sine=frequency=440:duration=1', resolve(outDir, 'fixture.wav')], { stdio: 'ignore' });
execFileSync('ffmpeg', ['-y', '-f', 'lavfi', '-i', 'color=c=red:s=64x64:d=1', '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-movflags', '+faststart', resolve(outDir, 'fixture.mp4')], { stdio: 'ignore' });
const audio = 'data:audio/wav;base64,' + (await readFile(resolve(outDir, 'fixture.wav'))).toString('base64');
const video = 'data:video/mp4;base64,' + (await readFile(resolve(outDir, 'fixture.mp4'))).toString('base64');
execFileSync('ffmpeg', ['-y', '-f', 'lavfi', '-i', 'color=c=red:s=16x16:d=0.1', '-frames:v', '1', '-pix_fmt', 'rgb24', '-c:v', 'jpeg2000', '-format', 'jp2', resolve(outDir, 'fixture.jp2')], { stdio: 'ignore' });
const jp2 = (await readFile(resolve(outDir, 'fixture.jp2'))).toString('base64');
await build({
  configFile: false,
  define: { __COGNIS_APPLE_BUNDLE__: String(native) },
  worker: { plugins: () => native ? [nativePdfWorker()] : [] },
  resolve: { alias: { '$lib': resolve('src/lib') } },
  plugins: [svelte({ configFile: false, preprocess: vitePreprocess() }), {
    name: 'artifact-preview-fixture',
    transform(code, id) {
      if (id.endsWith('/artifact-preview-fixture.ts')) {
        return code.replace('__AUDIO_DATA_URL__', audio).replace('__VIDEO_DATA_URL__', video).replace('__JPEG2000_BASE64__', jp2);
      }
    },
    resolveId(id) {
      if (['$app/environment', '$app/paths', '$env/dynamic/public'].includes(id)) return '\0fixture:' + id;
    },
    load(id) {
      if (id === '\0fixture:$app/environment') return 'export const browser = true; export const dev = false;';
      if (id === '\0fixture:$env/dynamic/public') return 'export const env = {};';
      if (id === '\0fixture:$app/paths') return `export const base = ""; export const assets = ${JSON.stringify(native ? 'cognisbundle://app' : '')};`;
    },
  }],
  build: {
    outDir, emptyOutDir: false, target: 'safari17', minify: false, assetsInlineLimit: Infinity,
    lib: { entry: resolve('scripts/artifact-preview-fixture.ts'), name: 'ArtifactPreviewFixture', formats: ['iife'], fileName: () => 'fixture.js', cssFileName: 'fixture' },
    rollupOptions: { output: { inlineDynamicImports: true } },
  },
});
await writeFile(resolve(outDir, 'index.html'),
  `<!doctype html><html><head><link rel="stylesheet" href="${native ? 'cognisbundle://app/fixture.css' : './fixture.css'}"></head><body><!-- __sveltekit_test.app.start( -->` +
  '<div id="fixture"></div><script>' +
  (await readFile(resolve(outDir, 'fixture.js'), 'utf8')).replaceAll('</script', '<\\/script') +
  '</script></body></html>');
for (const dir of ['cmaps', 'standard_fonts', 'wasm']) {
  await cp(resolve('node_modules/pdfjs-dist', dir), resolve(outDir, 'pdfjs', dir), { recursive: true });
}
