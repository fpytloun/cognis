import { describe, expect, it } from 'vitest';

import { resolveKnowledgeTab } from './tabs';

describe('resolveKnowledgeTab', () => {
  it('preserves owner management deep links', () => {
    expect(resolveKnowledgeTab('access', 'owner', false)).toBe('access');
    expect(resolveKnowledgeTab('settings', 'owner', false)).toBe('settings');
  });

  it('redirects shared and viewer management deep links to Browse', () => {
    expect(resolveKnowledgeTab('settings', 'shared', false)).toBe('browse');
    expect(resolveKnowledgeTab('access', 'owner', true)).toBe('browse');
  });
});
