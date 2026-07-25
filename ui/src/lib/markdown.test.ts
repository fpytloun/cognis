import { describe, expect, it } from 'vitest';

import {
  createMarkdownStreamer,
  extractMarkdownHeadings,
  renderDocsMarkdown,
  renderInlineMarkdown,
  renderInlineMarkdownNoLinks,
  renderMarkdown,
  renderMarkdownDocument,
  stripMarkdown,
} from '$lib/markdown';

describe('renderMarkdown', () => {
  const preCount = (html: string) => html.match(/<pre\b/g)?.length ?? 0;

  it('renders inline code without markdown delimiters in the HTML output', () => {
    const html = renderMarkdown('Use `git status` before committing.');

    expect(html).toContain('<code>git status</code>');
    expect(html).not.toContain('`git status`');
  });

  it('renders bold text as strong markup', () => {
    const html = renderMarkdown('This is **text**.');

    expect(html).toContain('<strong>text</strong>');
    expect(html).not.toContain('**text**');
  });

  it('renders combined inline markdown while linkifying bare URLs once', () => {
    const html = renderMarkdown('Use **bold**, *italic*, `code`, and https://example.com/docs.');

    expect(html).toContain('<strong>bold</strong>');
    expect(html).toContain('<em>italic</em>');
    expect(html).toContain('<code>code</code>');
    expect(html.match(/<a\b/g)?.length ?? 0).toBe(1);
    expect(html).toContain('href="https://example.com/docs"');
  });

  it('renders raw html literally without creating active elements', () => {
    const html = renderMarkdown(
      '<script>alert(1)</script><a href="javascript:alert(1)">bad</a><img src="x" onerror="alert(1)" />'
    );

    expect(html).not.toContain('<script');
    expect(html).not.toContain('<img');
    expect(html).not.toContain('<a href=');
    expect(html).toContain('&lt;script&gt;alert(1)&lt;/script&gt;');
    expect(html).toContain('&lt;img src=&quot;x&quot;');
  });

  it('opens outgoing markdown links in a new tab', () => {
    const html = renderMarkdown('[Open](https://example.com) and [local](/settings)');

    expect(html).toContain('href="https://example.com"');
    expect(html).toContain('target="_blank"');
    expect(html).toContain('rel="noopener noreferrer"');
    expect(html).toContain('href="/settings"');
  });

  it('linkifies bare http and https URLs', () => {
    const html = renderMarkdown('See https://example.com/docs and http://example.net.');

    expect(html).toContain('href="https://example.com/docs"');
    expect(html).toContain('href="http://example.net"');
    expect(html).toContain('target="_blank"');
    expect(html).toContain('rel="noopener noreferrer"');
  });

  it('preserves query strings and fragments in bare URLs', () => {
    const html = renderMarkdown('Open https://example.com/search?q=a&lang=en#top');

    expect(html).toContain('href="https://example.com/search?q=a&amp;lang=en#top"');
  });

  it('does not double-linkify existing markdown links', () => {
    const html = renderMarkdown('[docs](https://example.com/docs)');

    expect(html.match(/<a\b/g)?.length ?? 0).toBe(1);
    expect(html).toContain('href="https://example.com/docs"');
  });

  it('does not linkify URL labels inside existing markdown links', () => {
    const html = renderMarkdown('[https://example.com](https://docs.example)');

    expect(html.match(/<a\b/g)?.length ?? 0).toBe(1);
    expect(html).toContain('href="https://docs.example"');
    expect(html).toContain('>https://example.com</a>');
  });

  it('does not linkify URLs inside inline or fenced code', () => {
    const inline = renderMarkdown('Use `https://example.com` in config.');
    const fenced = renderMarkdown(['```text', 'https://example.com', '```'].join('\n'));

    expect(inline).toContain('<code>https://example.com</code>');
    expect(inline).not.toContain('href="https://example.com"');
    expect(fenced).toContain('https://example.com');
    expect(fenced).not.toContain('href="https://example.com"');
  });

  it('strips trailing punctuation from bare URL hrefs while preserving text', () => {
    const html = renderMarkdown('Visit https://example.com/docs. Also (https://example.com/a_(b)).');

    expect(html).toContain('href="https://example.com/docs"');
    expect(html).toContain('https://example.com/docs</a>.');
    expect(html).toContain('href="https://example.com/a_(b)"');
    expect(html).toContain('https://example.com/a_(b)</a>).');
  });

  it('does not linkify unsafe or protocol-relative bare URLs', () => {
    const html = renderMarkdown('No javascript:alert(1), data:text/html,x, file:///etc/passwd or //example.com');

    expect(html).not.toContain('<a');
  });

  it('linkifies bare URLs through the streaming renderer', () => {
    const streamer = createMarkdownStreamer();
    const rendered = streamer.render('Stream https://example.com/live');
    const finalized = streamer.finalize('Stream https://example.com/live');

    expect(rendered).toContain('href="https://example.com/live"');
    expect(finalized).toContain('href="https://example.com/live"');
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

  it('highlights fenced code blocks with known languages', () => {
    const html = renderMarkdown(['```python', 'def hello():', '    return "world"', '```'].join('\n'));

    expect(html).toContain('class="hljs language-python"');
    expect(html).toContain('hljs-keyword');
    expect(html).toContain('hello');
  });

  it('keeps unknown-language code blocks readable and escaped', () => {
    const html = renderMarkdown(['```not-a-real-language', '<script>alert("x")</script>', '```'].join('\n'));

    expect(html).toContain('class="language-not-a-real-language"');
    expect(html).toContain('&lt;script&gt;alert("x")&lt;/script&gt;');
    expect(html).not.toContain('<script>');
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

  it('renders malformed LLM-style tables when the separator row is missing trailing cells', () => {
    const html = renderMarkdown([
      '| Phase | Work | Branch/worktree | Parallel? |',
      '|---|---|--|',
      '| A | WS1 + WS2 (same files, one coherent change set) | fix/projection-critical-pressure | no — foundation |',
      '| B1 | WS3 (prefix stability) | feat/projection-prefix-stability on top of A | parallel with B2 |',
    ].join('\n'));

    expect(html).toContain('<table>');
    expect(html).toContain('<th>Parallel?</th>');
    expect(html).toContain('<td>parallel with B2</td>');
    expect(html).toContain('markdown-table-wrap');
    expect(html).not.toContain('<p>| Phase | Work');
  });

  it('renders markdown documents with stable heading anchors', () => {
    const document = renderMarkdownDocument([
      '# Final report',
      '',
      '```md',
      '## Ignored fenced heading',
      '```',
      '',
      '## Stage scope',
      '',
      '## Stage scope',
    ].join('\n'), 'deliverable-dlv-1');

    expect(document.headings).toEqual([
      { id: 'deliverable-dlv-1-final-report', level: 1, text: 'Final report' },
      { id: 'deliverable-dlv-1-stage-scope', level: 2, text: 'Stage scope' },
      { id: 'deliverable-dlv-1-stage-scope-2', level: 2, text: 'Stage scope' },
    ]);
    expect(document.html).toContain('<h1 id="deliverable-dlv-1-final-report"');
    expect(document.html).toContain('<h2 id="deliverable-dlv-1-stage-scope"');
    expect(document.html).not.toContain('<h2 id="deliverable-dlv-1-ignored-fenced-heading"');
  });

  it('extracts markdown headings without including fenced code', () => {
    expect(extractMarkdownHeadings('## Visible\n\n```\n# Hidden\n```', 'doc')).toEqual([
      { id: 'doc-visible', level: 2, text: 'Visible' },
    ]);
  });
});

describe('renderInlineMarkdown', () => {
  it('renders an inline link without wrapping it in a <p>', () => {
    const html = renderInlineMarkdown('See [ČTK](https://example.com/ctk) for details.');

    expect(html).toContain('href="https://example.com/ctk"');
    expect(html).toContain('target="_blank"');
    expect(html).toContain('rel="noopener noreferrer"');
    expect(html).not.toContain('<p>');
  });

  it('renders inline bold/italic/code without block wrapping', () => {
    const html = renderInlineMarkdown('**bold** and _italic_ and `code`');

    expect(html).toBe('<strong>bold</strong> and <em>italic</em> and <code>code</code>');
  });

  it('escapes plain text with no markdown syntax unchanged (aside from HTML escaping)', () => {
    expect(renderInlineMarkdown('Plain text, no markup.')).toBe('Plain text, no markup.');
  });

  it('returns an empty string for empty input without throwing', () => {
    expect(renderInlineMarkdown('')).toBe('');
  });

  it('sanitizes unsafe raw HTML the same way renderMarkdown does (escaped, not executable)', () => {
    const html = renderInlineMarkdown('<img src=x onerror="alert(1)">Text');

    // Raw HTML embedded in markdown source is escaped to visible text by
    // the shared renderer (same as renderMarkdown), never parsed as a live
    // element -- there must be no actual `<img` element in the output.
    expect(html).not.toContain('<img');
    expect(html).toContain('Text');
  });

  it('linkifies bare URLs like renderMarkdown', () => {
    const html = renderInlineMarkdown('Visit https://example.com now.');

    expect(html).toContain('href="https://example.com"');
    expect(html).toContain('>https://example.com</a>');
  });
});

describe('renderInlineMarkdownNoLinks', () => {
  it('renders a markdown link as its label only, dropping the anchor', () => {
    const html = renderInlineMarkdownNoLinks('See [ČTK](https://example.com/ctk) for details.');

    expect(html).not.toContain('<a');
    expect(html).not.toContain('href');
    expect(html).toContain('ČTK');
  });

  it('preserves nested inline formatting inside the dropped link label', () => {
    const html = renderInlineMarkdownNoLinks('[**bold label**](https://example.com)');

    expect(html).toBe('<strong>bold label</strong>');
  });

  it('still renders bold/italic/code when there is no link', () => {
    expect(renderInlineMarkdownNoLinks('**bold** and `code`')).toBe(
      '<strong>bold</strong> and <code>code</code>',
    );
  });

  it('does not linkify bare URLs either', () => {
    const html = renderInlineMarkdownNoLinks('Visit https://example.com now.');

    expect(html).not.toContain('<a');
    expect(html).toContain('https://example.com');
  });

  it('returns an empty string for empty input without throwing', () => {
    expect(renderInlineMarkdownNoLinks('')).toBe('');
  });
});

describe('stripMarkdown', () => {
  it('turns markdown into readable preview text', () => {
    const text = stripMarkdown(['# Result', '', '**Done** with [docs](https://example.com).', '- item'].join('\n'));

    expect(text).toBe('Result\n\nDone with docs.\nitem');
  });
});
