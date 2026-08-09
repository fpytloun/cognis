import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { describe, expect, it } from 'vitest';

const childSource = readFileSync(resolve('src/lib/components/chat-v2/ChildChatView.svelte'), 'utf8');
const pageSource = readFileSync(resolve('src/routes/(app)/chat/[conversationId]/+page.svelte'), 'utf8');

describe('ChildChatView shell ownership', () => {
  it('uses the shared scoped timeline, live follow, and a real Inspector toggle', () => {
    expect(childSource).toContain('<ScopedChatV2Timeline');
    expect(childSource).toContain('<TimelineOngoingWorkDrawer');
    expect(childSource).toContain('childViewScope(view)');
    expect(childSource).toContain('Following latest');
    expect(childSource).toContain('data-testid="child-header-inspector"');
    expect(childSource).not.toContain('SessionDetailsButton');
    expect(childSource).not.toContain('aria-label="Info"');
  });

  it('mounts only in the middle grid column and leaves Inspector independent', () => {
    expect(pageSource).toContain('data-testid="child-middle-column"');
    expect(pageSource).toContain("inspectorPinned ? 'relative col-start-1 row-start-1 row-end-4' : 'absolute inset-0'");
    expect(pageSource.indexOf('<ConversationInfoDrawer')).toBeLessThan(
      pageSource.indexOf('data-testid="child-middle-column"'),
    );
    expect(pageSource).not.toContain('Sub-session drawer overlay');
    expect(pageSource).not.toContain('absolute inset-0 z-30');
  });
});
