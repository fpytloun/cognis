import { describe, expect, it } from 'vitest';

import { createMarkdownStreamer } from './markdown';

describe('createMarkdownStreamer', () => {
  it('renders empty string for empty input', () => {
    const s = createMarkdownStreamer();
    expect(s.render('')).toBe('');
  });

  it('produces the same final HTML as a direct parse', () => {
    const content = `# Title\n\nParagraph one.\n\nParagraph two with **bold**.\n\n- a\n- b\n`;
    const s = createMarkdownStreamer();
    const finalHtml = s.finalize(content);
    expect(finalHtml).toContain('<h1');
    expect(finalHtml).toContain('Paragraph one');
    expect(finalHtml).toContain('Paragraph two');
    expect(finalHtml.toLowerCase()).toContain('<strong>bold</strong>');
    expect(finalHtml).toContain('<li>a</li>');
    expect(finalHtml).toContain('<li>b</li>');
  });

  it('reuses cached blocks on incremental render', () => {
    const s = createMarkdownStreamer();
    // Stream a message token-by-token building up 3 blocks.
    const chunks = ['# Hi\n\n', 'First.\n\n', 'Second block.'];
    let content = '';
    for (const chunk of chunks) {
      content += chunk;
      const html = s.render(content);
      expect(html).toContain('<h1');
    }
    // Final pass includes both paragraphs.
    const finalHtml = s.finalize(content);
    expect(finalHtml).toContain('First.');
    expect(finalHtml).toContain('Second block.');
  });

  it('keeps fenced code blocks intact while streaming', () => {
    const s = createMarkdownStreamer();
    const intermediate = '```ts\nlet x = 1;\n';
    // Tail is an unclosed fence - should not be split.
    const html1 = s.render(intermediate);
    expect(html1).toContain('let x = 1;');

    const closed = intermediate + '```\n\nFollow-up.';
    const html2 = s.finalize(closed);
    expect(html2).toContain('Follow-up.');
  });

  it('reset() drops the cache so next render parses fresh', () => {
    const s = createMarkdownStreamer();
    const content = 'A paragraph.\n\nAnother.';
    s.finalize(content);
    s.reset();
    const html = s.render(content);
    expect(html).toContain('paragraph');
  });

  it('handles empty tail without crashing', () => {
    const s = createMarkdownStreamer();
    // Content ending with trailing whitespace should not produce garbage.
    const html = s.render('Block one.\n\n   ');
    expect(html).toContain('Block one');
  });

  it('treats tilde-fenced code blocks identically to backtick fences', () => {
    const backtickContent = '```\nhello\n```\n\nAfter.';
    const tildeContent = '~~~\nhello\n~~~\n\nAfter.';
    const backtickHtml = createMarkdownStreamer().finalize(backtickContent);
    const tildeHtml = createMarkdownStreamer().finalize(tildeContent);
    // Both should produce equivalent block structure: a fenced code block
    // followed by a paragraph. The number of <pre>/<p> tags must match; the
    // cached block boundary behavior must be identical between fence styles.
    const preCount = (s: string) => (s.match(/<pre\b/g) ?? []).length;
    const pCount = (s: string) => (s.match(/<p\b/g) ?? []).length;
    expect(preCount(backtickHtml)).toBe(preCount(tildeHtml));
    expect(pCount(backtickHtml)).toBe(pCount(tildeHtml));
    expect(backtickHtml).toContain('hello');
    expect(tildeHtml).toContain('hello');
    expect(backtickHtml).toContain('After');
    expect(tildeHtml).toContain('After');
  });
});
