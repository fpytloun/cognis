import { describe, expect, it } from 'vitest';

import type { BackgroundWorkItem, Conversation } from '$lib/types/api';
import { partitionSidebarConversationLanes } from './conversation-sidebar';

function conversation(
  conversation_id: string,
  values: Partial<Conversation> = {},
): Conversation {
  return {
    conversation_id,
    has_active_turn: false,
    has_unread: false,
    pending_notification_types: [],
    ...values,
  } as Conversation;
}

describe('partitionSidebarConversationLanes', () => {
  it.each([[], ['step_question'], ['approval_required'], ['auth_request']])(
    'keeps unread shared readers in Recent, including stale source actions (%s)',
    (...pending_notification_types) => {
      const shared = conversation('shared', {
        is_readonly_for_caller: true, has_unread: true,
        pending_notification_types,
      });
      expect(partitionSidebarConversationLanes([shared])).toEqual({
        active: [], waiting: [], recent: [shared],
      });
      expect(shared.has_unread).toBe(true);
    },
  );

  it('keeps genuinely running shared conversations in Active without source input requests', () => {
    const shared = conversation('shared', {
      is_readonly_for_caller: true, has_active_turn: true, has_unread: true,
      pending_notification_types: ['step_question'],
    });
    expect(partitionSidebarConversationLanes([shared])).toEqual({
      active: [shared], waiting: [], recent: [],
    });
  });

  it.each([false, true])('puts attached running work in Active (unread=%s)', (has_unread) => {
    const owner = conversation('owner', { has_unread });
    const work = { kind: 'managed_conversation', status: 'running', controller_conversation_id: 'owner' } as BackgroundWorkItem;
    expect(partitionSidebarConversationLanes([owner], [work])).toEqual({
      active: [owner], waiting: [], recent: [],
    });
    expect(partitionSidebarConversationLanes([owner], [{ ...work, status: 'completed' }])).toEqual({
      active: [], waiting: has_unread ? [owner] : [], recent: has_unread ? [] : [owner],
    });
  });

  it('does not promote other conversations and preserves blocked precedence', () => {
    const owner = conversation('owner', { pending_notification_types: ['step_question'] });
    const other = conversation('other');
    const work = { kind: 'managed_conversation', status: 'running', controller_conversation_id: 'owner' } as BackgroundWorkItem;
    expect(partitionSidebarConversationLanes([owner, other], [work])).toEqual({
      active: [], waiting: [owner], recent: [other],
    });
  });

  it('keeps active unread work in Active', () => {
    const active = conversation('active', { has_active_turn: true, has_unread: true });
    expect(partitionSidebarConversationLanes([active])).toEqual({
      active: [active],
      waiting: [],
      recent: [],
    });
  });

  it('moves inactive unread work to Waiting', () => {
    const unread = conversation('unread', { has_unread: true });
    expect(partitionSidebarConversationLanes([unread])).toEqual({
      active: [],
      waiting: [unread],
      recent: [],
    });
  });

  it('keeps a blocked active turn in Waiting', () => {
    const blocked = conversation('blocked', {
      has_active_turn: true,
      pending_notification_types: ['step_question'],
    });
    expect(partitionSidebarConversationLanes([blocked])).toEqual({
      active: [],
      waiting: [blocked],
      recent: [],
    });
  });
});
