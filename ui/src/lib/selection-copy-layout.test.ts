import fs from 'node:fs';
import path from 'node:path';
import { describe, expect, it } from 'vitest';

const source = fs.readFileSync(path.resolve('src/routes/+layout.svelte'), 'utf8');

describe('root selection-copy listener contract', () => {
  it('installs the global listener once and removes it during root layout cleanup', () => {
    expect(source.match(/const uninstallSelectionCopy = installSelectionCopy\(\);/g)).toHaveLength(1);
    expect(source.match(/^\s+uninstallSelectionCopy\(\);$/gm)).toHaveLength(1);
  });
});
