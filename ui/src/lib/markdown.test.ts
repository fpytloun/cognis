import { describe, expect, it } from 'vitest';

import { renderMarkdown } from '$lib/markdown';

describe('renderMarkdown', () => {
  it('strips dangerous html and event handlers', () => {
    const html = renderMarkdown(
      '<script>alert(1)</script><a href="javascript:alert(1)">bad</a><img src="x" onerror="alert(1)" />'
    );

    expect(html).not.toContain('<script');
    expect(html).not.toContain('onerror');
    expect(html).not.toContain('javascript:');
  });
});
