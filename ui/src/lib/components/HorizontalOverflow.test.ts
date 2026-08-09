import { readFileSync } from 'node:fs';
import { describe, expect, it } from 'vitest';

const source = (relative: string) => readFileSync(new URL(relative, import.meta.url), 'utf8');

describe('horizontal overflow utility', () => {
  it.each([
    ['./work/WorkView.svelte', 'Work session labels'],
    ['./work/WorkFileTree.svelte', 'Work file paths'],
    ['./timeline/TimelineTodoDrawer.svelte', 'todo labels'],
    ['./timeline/TimelineOngoingWorkDrawer.svelte', 'ongoing work labels'],
  ])('uses hidden-scroll horizontal labels in %s (%s)', (path) => {
    const content = source(path);
    expect(content).toContain('scrollbar-hidden-x');
  });

  it('defines Firefox and WebKit scrollbar hiding once', () => {
    const css = source('../../app.css');
    expect(css).toContain('.scrollbar-hidden-x');
    expect(css).toContain('scrollbar-width: none');
    expect(css).toContain('::-webkit-scrollbar');
  });

  it('keeps chat markdown tables wide inside a touch-scroll container', () => {
    const css = source('../../app.css');
    expect(css).toMatch(
      /\.chat-markdown \.markdown-table-wrap\s*\{[^}]*overflow-x:\s*auto;[^}]*overscroll-behavior-x:\s*contain;[^}]*-webkit-overflow-scrolling:\s*touch;[^}]*\}/s
    );
    expect(css).toMatch(
      /\.chat-markdown \.markdown-table-wrap table\s*\{[^}]*display:\s*table;[^}]*width:\s*max-content;[^}]*min-width:\s*100%;[^}]*max-width:\s*none;[^}]*\}/s
    );
    expect(css).toMatch(
      /\.chat-markdown \.markdown-table-wrap :is\(th, td\)\s*\{[^}]*min-width:\s*8rem;[^}]*overflow-wrap:\s*normal;[^}]*word-break:\s*normal;[^}]*\}/s
    );
  });
});
