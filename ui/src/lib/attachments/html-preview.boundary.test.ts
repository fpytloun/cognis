import { build } from 'vite';
import { resolve } from 'node:path';
import { describe, expect, it } from 'vitest';

describe('standalone HTML mounting module boundary', () => {
  it('has no private API, auth, lease-coordination or private-resolver input in any chunk', async () => {
    let inputs: string[] = [];
    await build({ configFile: false, logLevel: 'silent',
      resolve: { alias: { $lib: resolve('src/lib') } },
      plugins: [{ name: 'inspect-safe-html-inputs', generateBundle() { inputs = [...this.getModuleIds()]; } }],
      build: { write: false, minify: false,
        lib: { entry: resolve('src/lib/attachments/html-preview.ts'), formats: ['es'], fileName: 'safe-html' } } });
    expect(inputs.length).toBeGreaterThan(0);
    expect(inputs.join('\n')).not.toMatch(/api\/client|stores\/auth|document-coordination|html-preview-private/);
  });
});
