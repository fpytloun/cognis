import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { cleanup, fireEvent, render, screen } from '@testing-library/svelte';
import { afterEach, describe, expect, it, vi } from 'vitest';
import SessionDetailsButton from './components/session/SessionDetailsButton.svelte';

const pageSource = readFileSync(
  resolve('src/routes/(app)/chat/[conversationId]/+page.svelte'),
  'utf8',
);

afterEach(cleanup);

describe('mobile chat header contract', () => {
  it('keeps the canonical inspector action inside the single title header', () => {
    const headerStart = pageSource.indexOf('data-testid="chat-header"');
    const headerEnd = pageSource.indexOf('{#if chatSearchOpen}', headerStart);
    const header = pageSource.slice(headerStart, headerEnd);

    expect(headerStart).toBeGreaterThan(-1);
    expect(headerEnd).toBeGreaterThan(headerStart);
    expect(header).toContain('data-testid="chat-header-controls"');
    expect(header).toContain('<SessionDetailsButton ');
    expect(header).toContain('testId="chat-header-info"');
    expect(header).toContain('ariaControls="conversation-info-drawer"');
    expect(header).toContain('onclick={toggleHeaderInfo}');
    expect(header).not.toContain('conversation-mobile-inspector-control');
    expect(header.match(/testId="chat-header-info"/g)).toHaveLength(1);
  });

  it('renders the shared inspector action with the drawer binding and toggle semantics', async () => {
    const onclick = vi.fn();
    const view = render(SessionDetailsButton, {
      open: false, ariaControls: 'conversation-info-drawer', testId: 'chat-header-info', onclick,
    });
    const button = screen.getByRole('button', { name: 'Toggle session details' });
    expect(screen.getAllByTestId('chat-header-info')).toHaveLength(1);
    expect(button).toHaveAttribute('aria-controls', 'conversation-info-drawer');
    expect(button).toHaveAttribute('aria-expanded', 'false');
    expect(button).toHaveClass('touch-target-compact');
    await fireEvent.click(button);
    expect(onclick).toHaveBeenCalledTimes(1);
    await view.rerender({ open: true });
    expect(button).toHaveAttribute('aria-expanded', 'true');
  });

  it('keeps header spacing responsive and delegates status-area ownership to shared CSS', () => {
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

  it('uses a compact expandable toolbar and automatic conversation paging', () => {
    expect(pageSource).not.toContain('>Conversations</p>');
    expect(pageSource).toContain('aria-label="Conversation controls"');
    expect(pageSource).toContain('role="group" aria-label="Conversation controls"');
    expect(pageSource).toContain('<div class="flex items-center gap-1">');
    expect(pageSource).toContain('Expand conversation filters');
    expect(pageSource).toContain("lucide-svelte/icons/filter");
    expect(pageSource).not.toContain("lucide-svelte/icons/sliders-horizontal");
    expect(pageSource).toContain('Expand conversation search');
    expect(pageSource).not.toContain('>Filters</span>');
    expect(pageSource).toContain('<Tooltip placement="bottom" text="Refresh conversations" showOnTouch={false}>');
    expect(pageSource).toContain('<Tooltip placement="bottom" text="New conversation" showOnTouch={false}>');
    expect(pageSource).toContain('showOnTouch={false}');
    expect(pageSource).toContain('use:observeConversationListEnd');
    expect(pageSource).not.toContain('Load more conversations');
  });

  it('emphasizes new conversation as the primary toolbar action', () => {
    const label = pageSource.indexOf('aria-label="New conversation"');
    const button = pageSource.slice(pageSource.lastIndexOf('<button', label), pageSource.indexOf('</button>', label));
    expect(button).toContain('h-9 w-9');
    expect(button).toContain('bg-accent-subtle text-accent');
    expect(button).toContain('<Plus class="h-4 w-4" strokeWidth={2.5}');
    expect(button).toContain('focus-visible:ring-2');
    expect(button).toContain('disabled={newChatCreating}');
    expect(button).toContain('onclick={openNewConversationModal}');
  });

  it('presents direct agent chats as a peer section with one topic boundary', () => {
    const agentsHeading = pageSource.indexOf('aria-labelledby="sidebar-agents-heading"');
    const agentsSection = pageSource.slice(
      pageSource.lastIndexOf('<section', agentsHeading),
      pageSource.indexOf('{/if}', agentsHeading),
    );
    expect(agentsSection).toContain('>Agents</p>');
    expect(agentsSection).toContain('border-b');
    expect(agentsSection).not.toContain('border-t');
    expect(agentsSection).toContain('pb-0');
    expect(agentsSection).toContain('tracking-[0.24em]');
  });

  it('shows subtle right-aligned counts for active and waiting conversations', () => {
    expect(pageSource).toContain('visibleConversationLanes.active.length} active conversations');
    expect(pageSource).toContain('visibleConversationLanes.waiting.length} waiting conversations');
    expect(pageSource).toContain('bg-info-subtle');
    expect(pageSource).toContain('bg-warning-subtle');
    expect(pageSource).toContain('justify-between');
    expect(pageSource).toContain('tabular-nums');
  });

  it('uses one shared topic-conversation scroller on every viewport', () => {
    expect(pageSource).toContain("const root = node.closest<HTMLElement>('[data-pull-to-refresh-scroller]')");
    expect(pageSource).not.toContain('data-conversation-history-scroll');
    expect(pageSource).not.toContain('lg:max-h-44 lg:overflow-y-auto');
    expect(pageSource).not.toContain('lg:flex-1 lg:overflow-y-auto');
  });

  it('starts the timeline directly below the chat header separator at all widths', () => {
    expect(pageSource).toContain('px-2.5 pb-1.5 pt-0 sm:px-4 sm:pb-4"');
    expect(pageSource).not.toContain('px-2.5 py-1.5 sm:p-4');
    expect(pageSource).not.toContain('sm:p-4');
  });

  it('routes unread conversations into the waiting lane', () => {
    expect(pageSource).toContain('partitionSidebarConversationLanes(visibleConversationList, backgroundWork.items)');
    expect(pageSource).toContain('waitingReason={dashboardConversationWaitingReason(conversation)}');
    expect(pageSource).not.toContain("'Unread activity'");
  });

  it('renders History date labels as subtle secondary metadata', () => {
    const dateLabel = pageSource.match(/id=\{`history-section-\$\{section\.key\}`\} class="([^"]+)"/)?.[1] ?? '';
    expect(dateLabel).toContain('text-right');
    expect(dateLabel).toContain('italic');
    expect(dateLabel).toContain('text-muted');
    expect(dateLabel).not.toContain('bg-canvas');
    expect(dateLabel).not.toContain('rounded-lg');
  });
});
