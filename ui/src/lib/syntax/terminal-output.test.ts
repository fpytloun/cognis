import { describe, expect, it } from 'vitest';

import { renderTerminalOutput } from './terminal-output';

describe('terminal output rendering', () => {
  it('renders ANSI SGR colors without leaking escape sequences', () => {
    const html = renderTerminalOutput('\x1b[0m\x1b[0;38;5;240m\x1b[0m\x1b[0;37m[i] Code: TNXS-LGZP\x1b[0m');

    expect(html).toContain('[i] Code: TNXS-LGZP');
    expect(html).toContain('color: rgb(var(--color-foreground, 229 231 235))');
    expect(html).not.toContain('\x1b');
  });

  it('themes normal, bright and indexed ANSI foregrounds without changing truecolor fidelity', () => {
    const html = renderTerminalOutput('normal \x1b[37mwhite\x1b[97mbright\x1b[38;5;7mindexed\x1b[38;5;15mindexed bright\x1b[31merror\x1b[0m reset');
    expect(html).toContain('normal ');
    expect(html.match(/--color-foreground/g)).toHaveLength(4);
    expect(html).toContain('--color-danger');
    expect(html).toContain(' reset');
    expect(html).not.toContain('#e5e7eb');
    expect(html).not.toContain('#f9fafb');
  });

  it('supports truecolor and escapes unsafe HTML', () => {
    const html = renderTerminalOutput('\x1b[38;2;1;2;3m<script>alert("x")</script>\x1b[0m');

    expect(html).toContain('color: rgb(1, 2, 3)');
    expect(html).toContain('&lt;script&gt;alert(&quot;x&quot;)&lt;/script&gt;');
    expect(html).not.toContain('<script>');
  });

  it.each([
    ['30;47', '#111827', '#e5e7eb'],
    ['37;40', '#e5e7eb', '#111827'],
    ['38;5;0;48;5;7', '#111827', '#e5e7eb'],
    ['38;5;7;48;5;0', '#e5e7eb', '#111827'],
    ['47', '#111827', '#e5e7eb'],
    ['40', '#f9fafb', '#111827'],
    ['48;5;7', '#111827', '#e5e7eb'],
    ['48;5;0', '#f9fafb', '#111827'],
    ['30;47;7', '#e5e7eb', '#111827'],
  ])('preserves readable explicit ANSI pairs for %s', (codes, fg, bg) => {
    const html = renderTerminalOutput(`\x1b[${codes}mtext\x1b[0m reset`);
    expect(html).toContain(`color: ${fg}; background-color: ${bg}`);
    expect(html).toMatch(/<\/span> reset$/);
  });

  it('restores theme foregrounds after background and inverse resets', () => {
    const html = renderTerminalOutput('\x1b[37;40;7mreverse\x1b[27;49mtheme\x1b[0m reset');
    expect(html).toContain('color: #111827; background-color: #e5e7eb');
    expect(html).toContain('color: rgb(var(--color-foreground, 229 231 235))');
    expect(html).toMatch(/<\/span> reset$/);
  });

  it('linkifies bare http and https URLs in terminal output', () => {
    const html = renderTerminalOutput('Open https://login.example.com/start, then continue.');

    expect(html).toContain('<a href="https://login.example.com/start"');
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
