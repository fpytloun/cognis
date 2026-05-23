import { describe, expect, it } from 'vitest';

import { renderTerminalOutput } from './terminal-output';

describe('terminal output rendering', () => {
  it('renders ANSI SGR colors without leaking escape sequences', () => {
    const html = renderTerminalOutput('\x1b[0m\x1b[0;38;5;240m\x1b[0m\x1b[0;37m[i] Code: TNXS-LGZP\x1b[0m');

    expect(html).toContain('[i] Code: TNXS-LGZP');
    expect(html).toContain('color: #e5e7eb');
    expect(html).not.toContain('\x1b');
  });

  it('supports truecolor and escapes unsafe HTML', () => {
    const html = renderTerminalOutput('\x1b[38;2;1;2;3m<script>alert("x")</script>\x1b[0m');

    expect(html).toContain('color: rgb(1, 2, 3)');
    expect(html).toContain('&lt;script&gt;alert(&quot;x&quot;)&lt;/script&gt;');
    expect(html).not.toContain('<script>');
  });

  it('linkifies bare http and https URLs in terminal output', () => {
    const html = renderTerminalOutput('Open https://lumilens.awsapps.com/start, then continue.');

    expect(html).toContain('<a href="https://lumilens.awsapps.com/start"');
    expect(html).toContain('target="_blank"');
    expect(html).toContain('rel="noopener noreferrer"');
    expect(html).toContain('</a>, then continue.');
  });

  it('applies carriage-return overwrites for progress output', () => {
    const html = renderTerminalOutput('Downloading 10%\rDownloading 100%\nDone');

    expect(html).toBe('Downloading 100%\nDone');
    expect(html).not.toContain('10%');
  });

  it('applies clear-line control sequences used by git progress', () => {
    const html = renderTerminalOutput('Rebasing (1/2)\nRebasing (2/2)\r\x1b[KSuccessfully rebased and updated refs/heads/main.');

    expect(html).toBe('Rebasing (1/2)\nSuccessfully rebased and updated refs/heads/main.');
    expect(html).not.toContain('Rebasing (2/2)');
  });

  it('drops transient lines cleared before final output', () => {
    const html = renderTerminalOutput('Rebasing (1/2)\nRebasing (2/2)\r\x1b[K\nSuccessfully rebased and updated refs/heads/main.');

    expect(html).toBe('Rebasing (1/2)\nSuccessfully rebased and updated refs/heads/main.');
  });

  it('applies cursor movement and backspace edits', () => {
    const html = renderTerminalOutput('abc\x1b[2DXY\nspin-\b/');

    expect(html).toBe('aXY\nspin/');
  });
});
