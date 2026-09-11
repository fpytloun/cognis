import { describe, expect, it, vi } from 'vitest';

import {
  handleSelectionCopy,
  installSelectionCopy,
  serializeSelectionRange,
} from '$lib/selection-copy';

function rangeFor(element: Element): Range {
  const range = document.createRange();
  range.selectNodeContents(element);
  return range;
}

function payloadFor(html: string) {
  document.body.innerHTML = `<main>${html}</main>`;
  return serializeSelectionRange(rangeFor(document.querySelector('main')!));
}

function clipboardEvent(
  target: EventTarget = document.body,
  rejectMarkdown = false,
): { event: ClipboardEvent; data: Map<string, string> } {
  const data = new Map<string, string>();
  const event = {
    target,
    clipboardData: {
      setData(type: string, value: string) {
        if (rejectMarkdown && type === 'text/markdown') throw new Error('unsupported');
        data.set(type, value);
      },
    },
    defaultPrevented: false,
    preventDefault: vi.fn(),
  } as unknown as ClipboardEvent;
  return { event, data };
}

describe('selection copy serialization', () => {
  it('copies visible text without Markdown decoration, escaping, or link destinations', () => {
    const payload = payloadFor(
      '<h2>Title</h2><p><strong>Bold</strong> <a href="https://example.com">label</a> <code>file_name*</code></p><blockquote>Quote</blockquote>',
    );
    expect(payload.plainText).toBe('Title\n\nBold label file_name*\n\nQuote');
  });

  it('preserves plain code whitespace and textual list numbering', () => {
    expect(payloadFor('<pre>  file_name*\n\n\nnext  </pre>').plainText).toBe('  file_name*\n\n\nnext  ');
    expect(payloadFor('<ol start="3"><li>First</li><li>Second</li></ol>').plainText).toBe('3. First\n4. Second');
    expect(payloadFor('<ul><li>First<ul><li>Nested</li></ul></li></ul>').plainText).toBe('- First\n  - Nested');
  });

  it('does not add list markers or escape characters to selected words', () => {
    document.body.innerHTML = '<ul><li>file_name*</li></ul>';
    const text = document.querySelector('li')!.firstChild!;
    const range = document.createRange();
    range.setStart(text, 0);
    range.setEnd(text, 10);
    expect(serializeSelectionRange(range).plainText).toBe('file_name*');
  });

  it('removes visual styling and UI chrome while preserving semantic HTML', () => {
    const payload = payloadFor(`
      <section class="bg-slate-950" style="background:red;color:white">
        <h2>Result</h2><p><strong>Safe</strong> text</p>
        <button>Copy</button><svg><path d="x"></path></svg>
        <div data-copy-exclude>12:30 · actions</div>
      </section>
    `);

    expect(payload.html).toContain('<h2>Result</h2>');
    expect(payload.html).toContain('<strong>Safe</strong>');
    expect(payload.html).not.toMatch(/class=|style=|background|button|svg|12:30/);
    expect(payload.markdown).toContain('## Result');
    expect(payload.markdown).toContain('**Safe** text');
  });

  it('serializes nested unordered and ordered lists with normal markers', () => {
    const payload = payloadFor(`
      <ul><li>Alpha<ul><li>Nested</li></ul></li></ul>
      <ol><li>First</li><li>Second</li></ol>
    `);

    expect(payload.markdown).toContain('- Alpha\n  - Nested');
    expect(payload.markdown).toContain('1. First\n1. Second');
    expect(payload.markdown).not.toContain('•');
  });

  it('preserves links, headings, blockquotes, inline code, and fenced code', () => {
    const payload = payloadFor(`
      <h1>Title</h1>
      <p>Read <a href="https://example.com/path">docs</a> and <code>item.id</code>.</p>
      <blockquote><p>Quoted text</p></blockquote>
      <pre><code>const value = 1;</code></pre>
    `);

    expect(payload.markdown).toContain('# Title');
    expect(payload.markdown).toContain('[docs](https://example.com/path)');
    expect(payload.markdown).toContain('`item.id`');
    expect(payload.markdown).toContain('> Quoted text');
    expect(payload.markdown).toContain('```\nconst value = 1;\n```');
  });

  it('uses safe delimiters and preserves code whitespace', () => {
    const payload = payloadFor(`
      <p><code>value \`tick\`</code> and <code> value </code> and <code> </code></p>
      <pre><code>first${'  '}


fourth \`\`\`\`
</code></pre>
    `);

    expect(payload.markdown).toContain('`` value `tick` ``');
    expect(payload.markdown).toContain('`  value  `');
    expect(payload.markdown).toContain('and ` `');
    expect(payload.markdown).toContain('`````\nfirst  \n\n\nfourth ````\n`````');
  });

  it('uses tab-separated table text without decorative table borders', () => {
    const payload = payloadFor(`
      <table><thead><tr><th>Name</th><th>Value</th></tr></thead>
      <tbody><tr><td>alpha</td><td>1</td></tr></tbody></table>
    `);

    expect(payload.html).toContain('<table>');
    expect(payload.html).toContain('<th>Name</th>');
    expect(payload.markdown).toBe('Name\tValue\nalpha\t1');
    expect(payload.markdown).not.toMatch(/\+---|\|/);
  });

  it('reduces images to alt text and removes unsafe links', () => {
    const payload = payloadFor(`
      <p>Before <img src="https://private.example/token" alt="Architecture diagram"> after.</p>
      <a href="javascript:alert(1)">Unsafe</a>
    `);

    expect(payload.markdown).toContain('Before Architecture diagram after.');
    expect(payload.html).not.toContain('<img');
    expect(payload.html).not.toContain('private.example');
    expect(payload.html).not.toContain('javascript:');
  });

  it('does not leak a private URL from a linked image', () => {
    const payload = payloadFor(`
      <a href="/api/artifacts/private?token=secret">
        <img src="/api/artifacts/private?token=secret" alt="System diagram">
      </a>
    `);

    expect(payload.markdown).toBe('System diagram');
    expect(payload.html).toContain('System diagram');
    expect(payload.html).not.toContain('href');
    expect(payload.html).not.toContain('token=secret');
  });

  it('preserves multiline pre-wrap text and indentation', () => {
    const payload = payloadFor('<p class="whitespace-pre-wrap">line 1\n  line 2</p>');

    expect(payload.markdown).toBe('line 1\n  line 2');
    expect(payload.html).toBe('<pre>line 1\n  line 2</pre>');
  });

  it('preserves and escapes a partial multiline pre-wrap selection', () => {
    document.body.innerHTML =
      '<p class="whitespace-pre-wrap">prefix\n# literal\n- not a list\n1. not ordered\nsuffix</p>';
    const text = document.querySelector('p')!.firstChild!;
    const range = document.createRange();
    range.setStart(text, 7);
    range.setEnd(text, text.textContent!.length - 7);
    const payload = serializeSelectionRange(range);

    expect(payload.markdown).toBe('\\# literal\n\\- not a list\n\\1. not ordered');
    expect(payload.html).toBe('<pre># literal\n- not a list\n1. not ordered</pre>');
  });

  it('escapes literal Markdown punctuation and line-leading list markers', () => {
    const payload = payloadFor(`
      <p># literal *text* [label] &lt;tag&gt; ~value~</p>
      <p>- not a list<br>+ not a list<br>1. not ordered<br>1) not ordered<br>---</p>
    `);

    expect(payload.markdown).toContain('\\# literal \\*text\\* \\[label\\] \\<tag\\> \\~value\\~');
    expect(payload.markdown).toContain(
      '\\- not a list\n\\+ not a list\n\\1. not ordered\n\\1) not ordered\n\\---',
    );
  });

  it('keeps a meaningful linked-card destination while removing its image payload', () => {
    const payload = payloadFor(`
      <a href="https://example.com/article">
        <img src="/api/private-thumbnail?token=secret" alt="Thumbnail">
        Article title
      </a>
    `);

    expect(payload.html).toContain('href="https://example.com/article"');
    expect(payload.html).not.toContain('private-thumbnail');
    expect(payload.markdown).toContain('[Thumbnail Article title](https://example.com/article)');
  });

  it('removes marked non-interactive message chrome', () => {
    const payload = payloadFor(`
      <article><p>Message body</p>
        <div data-copy-exclude>Agent · 10:30 · Streaming</div>
      </article>
    `);

    expect(payload.markdown).toBe('Message body');
    expect(payload.html).not.toContain('10:30');
  });

  it('preserves partial selection boundaries', () => {
    document.body.innerHTML = '<p id="source">prefix selected suffix</p>';
    const text = document.querySelector('#source')!.firstChild!;
    const range = document.createRange();
    range.setStart(text, 7);
    range.setEnd(text, 15);

    expect(serializeSelectionRange(range)).toEqual({ html: 'selected', plainText: 'selected', markdown: 'selected' });
  });
});

describe('selection copy event handling', () => {
  it('writes semantic HTML and plain text without a Markdown clipboard representation', () => {
    document.body.innerHTML = '<p id="source">Selected <strong>text</strong></p>';
    const source = document.querySelector('#source')!;
    const selection = document.getSelection()!;
    selection.removeAllRanges();
    selection.addRange(rangeFor(source));
    const { event, data } = clipboardEvent(source, true);

    expect(handleSelectionCopy(event, selection)).toBe(true);
    expect(event.preventDefault).toHaveBeenCalledOnce();
    expect(data.get('text/html')).toContain('<strong>text</strong>');
    expect(data.get('text/plain')).toBe('Selected text');
    expect(data.has('text/markdown')).toBe(false);
  });

  it.each(['input', 'textarea', 'select', 'contenteditable'])('bypasses %s editors', (kind) => {
    document.body.innerHTML =
      kind === 'contenteditable'
        ? '<div contenteditable="true">Editable value</div>'
        : `<${kind}>Editable value</${kind}>`;
    const editor = document.body.firstElementChild!;
    const { event } = clipboardEvent(editor);
    const selection = {
      isCollapsed: false,
      rangeCount: 1,
      anchorNode: editor,
      focusNode: editor,
      getRangeAt: vi.fn(),
    } as unknown as Selection;

    expect(handleSelectionCopy(event, selection)).toBe(false);
    expect(event.preventDefault).not.toHaveBeenCalled();
  });

  it('allows native copy when clipboardData is missing', () => {
    const event = { clipboardData: null, preventDefault: vi.fn() } as unknown as ClipboardEvent;
    expect(handleSelectionCopy(event, document.getSelection())).toBe(false);
    expect(event.preventDefault).not.toHaveBeenCalled();
  });

  it('allows native copy for multiple selection ranges', () => {
    const { event } = clipboardEvent();
    const selection = {
      isCollapsed: false,
      rangeCount: 2,
    } as Selection;

    expect(handleSelectionCopy(event, selection)).toBe(false);
    expect(event.preventDefault).not.toHaveBeenCalled();
  });

  it('does not intercept copy events from explicit copy buttons', () => {
    document.body.innerHTML = '<button type="button">Copy</button>';
    const button = document.querySelector('button')!;
    const { event } = clipboardEvent(button);
    const selection = {
      isCollapsed: false,
      rangeCount: 1,
      anchorNode: button,
      focusNode: button,
      getRangeAt: vi.fn(),
    } as unknown as Selection;

    expect(handleSelectionCopy(event, selection)).toBe(false);
    expect(event.preventDefault).not.toHaveBeenCalled();
  });

  it('allows native copy when range serialization fails', () => {
    const { event } = clipboardEvent();
    const selection = {
      isCollapsed: false,
      rangeCount: 1,
      anchorNode: document.body,
      focusNode: document.body,
      getRangeAt: () => {
        throw new Error('selection changed');
      },
    } as unknown as Selection;

    expect(handleSelectionCopy(event, selection)).toBe(false);
    expect(event.preventDefault).not.toHaveBeenCalled();
  });

  it('installs one listener and removes the same listener', () => {
    const target = {
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
    };

    const cleanup = installSelectionCopy(target);
    expect(target.addEventListener).toHaveBeenCalledOnce();
    expect(target.addEventListener).toHaveBeenCalledWith('copy', expect.any(Function));
    cleanup();
    expect(target.removeEventListener).toHaveBeenCalledWith(
      'copy',
      target.addEventListener.mock.calls[0][1],
    );
  });
});
