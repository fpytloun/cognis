import { mkdir, mkdtemp, writeFile } from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { standaloneDependencyViolation } from './standalone-build-policy';
// The build policy is intentionally plain ESM so Node can execute it after Vite.
// @ts-expect-error JavaScript policy module has no declaration file.
import { checkStandaloneBuild } from '../scripts/check-standalone-build.mjs';

const mount = vi.fn();
vi.mock('svelte', async (importOriginal) => ({
  ...(await importOriginal<typeof import('svelte')>()),
  mount,
}));
vi.mock('$lib/components/rich/RichDeliverable.svelte', () => ({ default: {} }));

describe('standalone client', () => {
  beforeEach(() => {
    vi.resetModules();
    mount.mockReset();
    document.documentElement.innerHTML = '<head></head><body></body>';
    Object.defineProperty(window, 'matchMedia', {
      configurable: true,
      value: vi.fn(() => ({ matches: true })),
    });
  });

  it('reads inert template data and mounts only the rich deliverable', async () => {
    const template = document.createElement('template');
    template.id = 'cognis-deliverable-payload';
    template.dataset.mediaBase = '/api/media';
    template.dataset.pdfUrl = '/api/report.pdf';
    template.dataset.standaloneUrl = '/api/view';
    template.content.textContent = JSON.stringify({
      content: 'Fallback',
      instanceId: 'dlv_test',
      payload: { blocks: [{ type: 'card', title: 'Safe' }] },
      title: 'Report',
    });
    const root = document.createElement('div');
    root.id = 'cognis-deliverable-root';
    document.body.append(template, root);

    await import('./standalone');

    expect(mount).toHaveBeenCalledOnce();
    expect(mount.mock.calls[0][1]).toMatchObject({
      target: root,
      props: {
        content: 'Fallback',
        instanceId: 'dlv_test',
        pdfUrl: '/api/report.pdf',
        surface: 'standalone',
        title: 'Report',
      },
    });
    // Never passed: "Open standalone page" from the standalone page itself
    // would just reopen this same page.
    expect(mount.mock.calls[0][1].props.standaloneUrl).toBeUndefined();
    expect(mount.mock.calls[0][1].props.mediaUrlFor('media_abc')).toBe('/api/media/media_abc');
    expect(document.documentElement.dataset.resolvedTheme).toBe('dark');

    const link = document.createElement('a');
    link.target = '_blank';
    link.href = '/source';
    link.addEventListener('click', (event) => event.preventDefault());
    document.body.append(link);
    link.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true }));
    expect(link.target).toBe('_self');
  });
});

describe('standalone build policy', () => {
  it.each([
    ['/repo/ui/src/routes/+layout.svelte', 'forbidden SPA dependency'],
    ['/repo/ui/src/lib/stores/auth.ts', 'forbidden SPA dependency'],
    ['/repo/ui/src/lib/stores/chat-v2/store.svelte.ts', 'forbidden SPA dependency'],
    ['/repo/ui/node_modules/chart.js/auto.js', 'eagerly included lazy dependency'],
    ['/repo/ui/node_modules/mermaid/dist/mermaid.js', 'eagerly included lazy dependency'],
  ])('rejects %s', (moduleId, expected) => {
    expect(standaloneDependencyViolation([moduleId])).toContain(expected);
  });

  it('accepts the rich renderer and overlay support', () => {
    expect(standaloneDependencyViolation([
      '/repo/ui/src/lib/components/rich/RichDeliverable.svelte',
      '/repo/ui/src/lib/stores/overlays.ts',
    ])).toBeNull();
  });

  it('allows heavy dependencies only in lazy chunks', () => {
    expect(standaloneDependencyViolation(
      ['/repo/ui/node_modules/mermaid/dist/mermaid.js'],
      { allowLazyHeavy: true },
    )).toBeNull();
  });

  it('enforces the initial gzip budget from the Vite manifest', async () => {
    const buildDir = await mkdtemp(path.join(os.tmpdir(), 'cognis-standalone-'));
    await mkdir(path.join(buildDir, '.vite'), { recursive: true });
    await mkdir(path.join(buildDir, 'assets'), { recursive: true });
    await writeFile(path.join(buildDir, '.vite', 'manifest.json'), JSON.stringify({
      'src/standalone.ts': {
        file: 'assets/standalone-12345678.js',
        isEntry: true,
      },
    }));
    await writeFile(
      path.join(buildDir, 'assets', 'standalone-12345678.js'),
      Buffer.from(Array.from({ length: 4096 }, (_, index) => index % 251)),
    );

    await expect(checkStandaloneBuild({ buildDir, gzipBudgetBytes: 10 })).rejects.toThrow(
      /budget is 10 bytes/,
    );
  });
});
