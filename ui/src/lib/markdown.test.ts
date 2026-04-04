import { describe, expect, it } from 'vitest';

import { renderDocsMarkdown, renderMarkdown } from '$lib/markdown';

describe('renderMarkdown', () => {
  it('strips dangerous html and event handlers', () => {
    const html = renderMarkdown(
      '<script>alert(1)</script><a href="javascript:alert(1)">bad</a><img src="x" onerror="alert(1)" />'
    );

    expect(html).not.toContain('<script');
    expect(html).not.toContain('onerror');
    expect(html).not.toContain('javascript:');
  });

  it('wraps docs tables and code blocks in local overflow containers', () => {
    const html = renderDocsMarkdown([
      '```bash',
      'MNEMORY_JWT_PUBLIC_KEY=~/.cognis/keys/public.pem uvx mnemory',
      '```',
      '',
      '| Name | Description |',
      '| --- | --- |',
      '| VeryLongColumn | Value |',
    ].join('\n'));

    expect(html).toContain('markdown-code-wrap');
    expect(html).toContain('markdown-table-wrap');
    expect(html).toContain('<pre><code');
    expect(html).toContain('<table>');
  });
});
