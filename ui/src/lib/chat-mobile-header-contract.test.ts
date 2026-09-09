import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { describe, expect, it } from 'vitest';

const pageSource = readFileSync(
  resolve('src/routes/(app)/chat/[conversationId]/+page.svelte'),
  'utf8',
);

describe('mobile chat header contract', () => {
  it('keeps the canonical inspector action inside the single title header', () => {
    const headerStart = pageSource.indexOf('data-testid="chat-header"');
    const headerEnd = pageSource.indexOf('{#if chatSearchOpen}', headerStart);
    const header = pageSource.slice(headerStart, headerEnd);

    expect(headerStart).toBeGreaterThan(-1);
    expect(headerEnd).toBeGreaterThan(headerStart);
    expect(header).toContain('data-testid="chat-header-controls"');
    expect(header).toContain('data-testid="chat-header-info"');
    expect(header).toContain('aria-controls="conversation-info-drawer"');
    expect(header).toContain('touch-target-compact');
    expect(header).not.toContain('conversation-mobile-inspector-control');
    expect(header.match(/data-testid="chat-header-info"/g)).toHaveLength(1);
  });

  it('keeps the title header compact and delegates status-area spacing to the route frame', () => {
    const headerStart = pageSource.lastIndexOf('<div', pageSource.indexOf('data-testid="chat-header"'));
    const headerEnd = pageSource.indexOf('{#if chatSearchOpen}', headerStart);
    const header = pageSource.slice(headerStart, headerEnd);

    expect(header).toContain('lg:py-1.5');
    expect(header).toContain('sm:text-lg');
    expect(header).not.toContain('safe-area-inset-top');
  });

  it('omits redundant queue explanation, waiting, and position badges', () => {
    expect(pageSource).not.toContain('Current turn is still running.');
    expect(pageSource).not.toContain("committing ? 'committing' : 'waiting'");
    expect(pageSource).not.toContain('>#{queued.position}</span>');
  });

  it('starts and reopens mobile conversation history with filters collapsed', () => {
    expect(pageSource).toContain('conversationFiltersOpen = initialConversationFiltersOpen(window.innerWidth)');
    const openMobileList = pageSource.slice(
      pageSource.indexOf('function openMobileList(): void'),
      pageSource.indexOf('// Edge-swipe handlers', pageSource.indexOf('function openMobileList(): void')),
    );
    expect(openMobileList).toContain('conversationFiltersOpen = false');
  });
});
