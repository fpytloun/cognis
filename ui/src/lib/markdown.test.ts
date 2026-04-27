import { describe, expect, it } from 'vitest';

import { renderDocsMarkdown, renderMarkdown } from '$lib/markdown';

describe('renderMarkdown', () => {
  const preCount = (html: string) => html.match(/<pre\b/g)?.length ?? 0;

  it('renders inline code without markdown delimiters in the HTML output', () => {
    const html = renderMarkdown('Use `git status` before committing.');

    expect(html).toContain('<code>git status</code>');
    expect(html).not.toContain('`git status`');
  });

  it('strips dangerous html and event handlers', () => {
    const html = renderMarkdown(
      '<script>alert(1)</script><a href="javascript:alert(1)">bad</a><img src="x" onerror="alert(1)" />'
    );

    expect(html).not.toContain('<script');
    expect(html).not.toContain('onerror');
    expect(html).not.toContain('javascript:');
  });

  it('opens outgoing markdown links in a new tab', () => {
    const html = renderMarkdown('[Open](https://example.com) and [local](/settings)');

    expect(html).toContain('href="https://example.com"');
    expect(html).toContain('target="_blank"');
    expect(html).toContain('rel="noopener noreferrer"');
    expect(html).toContain('href="/settings"');
  });

  it('preserves malformed enclosing text fences that contain nested fenced examples', () => {
    const html = renderMarkdown(
      [
        '```text',
        'Task: Return everything inside one code block.',
        '',
        '```json',
        '{"quality_score": 0.92}',
        '```',
        '',
        'More instructions after the nested example.',
        '```',
      ].join('\n')
    );

    expect(preCount(html)).toBe(1);
    expect(html).toContain('```json');
    expect(html).toContain('"quality_score"');
    expect(html).toContain('More instructions after the nested example.');
    expect(html).not.toContain('<p>More instructions after the nested example.</p>');
  });

  it('does not merge ordinary non-generic consecutive code blocks', () => {
    const html = renderMarkdown(
      [
        '```ts',
        'const value = 1;',
        '```',
        '',
        '```json',
        '{"value": 1}',
        '```',
      ].join('\n')
    );

    expect(preCount(html)).toBe(2);
  });

  it('keeps valid long enclosing fences as a single code block', () => {
    const html = renderMarkdown(
      [
        '````text',
        '```json',
        '{"value": 1}',
        '```',
        '````',
      ].join('\n')
    );

    expect(preCount(html)).toBe(1);
    expect(html).toContain('```json');
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
