import { get } from 'svelte/store';
import { describe, expect, it } from 'vitest';

import { installPromptAvailable, updateAvailable } from './pwa';

describe('pwa stores', () => {
  it('installPromptAvailable defaults to false', () => {
    expect(get(installPromptAvailable)).toBe(false);
  });

  it('updateAvailable defaults to false', () => {
    expect(get(updateAvailable)).toBe(false);
  });
});
