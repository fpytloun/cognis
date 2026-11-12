import { describe, expect, it } from 'vitest';
import { appContactBindingId, shouldReplaceAgentSidebarConversation } from './app-contacts';
import { conversationMatchesSidebarProjectionFilter } from './chat-page';
import { isPrivateInstruction, managedChannelIdentity, remoteMessageSender } from './managed-channel';
import type { Conversation } from './types/api';
import { conversationFixture } from './testing/conversation-fixture';

function contact(id: string, lastMessage: string): Conversation {
  return conversationFixture({
    conversation_id: id, agent_id: 'contact-agent', status: 'active',
    context: { type: 'web', ref: id, memory_labels: {}, platform_data: { app_contact_binding_id: id } },
    last_message_at: lastMessage, has_unread: false, starred_at: null,
  });
}

describe('managed channel identity and provenance', () => {
  it('distinguishes channel work without classifying ordinary managed work as a channel', () => {
    const value = conversationFixture({
      context: { type: 'agent_work', ref: null, memory_labels: {}, platform_data: {
        managed_conversation_kind: 'channel', channel_type: 'web', participant: 'Sofia Novak',
      } },
      managed_agent: { channel: 'agent_work', kind: 'channel' },
    });
    expect(managedChannelIdentity(value)).toMatchObject({ channelLabel: 'Web', participant: 'Sofia Novak' });
    expect(managedChannelIdentity(conversationFixture({ managed_agent: { channel: 'agent_work', kind: 'agent' } }))).toBeNull();
  });

  it('separates private instructions from remote participant envelopes', () => {
    expect(isPrivateInstruction({ private_controller_instruction: true })).toBe(true);
    expect(isPrivateInstruction({ channel: 'agent_work', untrusted: false })).toBe(true);
    expect(remoteMessageSender({ channel: 'web', sender: 'Sofia Novak', untrusted: true })).toBe('Sofia Novak');
    expect(remoteMessageSender({ sender: 'Owner', untrusted: false })).toBeNull();
    expect(isPrivateInstruction({ channel: 'web', sender: 'Sofia Novak', untrusted: true })).toBe(false);
  });
});

describe('recipient sidebar persistence', () => {
  it('keeps an unstarred contact after its read update under the default starred topic filter', () => {
    const value = contact('contact-read', '2026-10-28T20:00:00Z');
    for (const unread of [true, false]) {
      expect(conversationMatchesSidebarProjectionFilter({ ...value, has_unread: unread }, {
        selectedChannels: [], selectedAgentIds: [], selectedConversationStatus: 'starred',
      })).toBe(true);
    }
    expect(appContactBindingId(value)).toBe('contact-read');
    expect(conversationMatchesSidebarProjectionFilter({ ...value, status: 'archived' }, {
      selectedChannels: [], selectedAgentIds: [], selectedConversationStatus: 'starred',
    })).toBe(false);
  });

  it('does not let reading an older contact replace a newer contact or a normal main chat', () => {
    const older = contact('contact-older', '2026-10-28T19:00:00Z');
    const newer = contact('contact-newer', '2026-10-28T20:00:00Z');
    const main = conversationFixture({ ...newer, context: { type: 'web', ref: null, memory_labels: {}, platform_data: {} } });
    expect(shouldReplaceAgentSidebarConversation(newer, older)).toBe(false);
    expect(shouldReplaceAgentSidebarConversation(older, newer)).toBe(true);
    expect(shouldReplaceAgentSidebarConversation(main, newer)).toBe(false);
    expect(shouldReplaceAgentSidebarConversation(newer, main)).toBe(true);
    expect(shouldReplaceAgentSidebarConversation(newer, { ...newer, has_unread: false })).toBe(true);
  });
});
