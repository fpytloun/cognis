import { describe, expect, it } from 'vitest';
import {
  entityActivityState,
  workspaceConversationForWindow,
  workspaceWindowActivityState,
  workspaceWindowTitle,
} from './workspace-activity';
import type { WorkspaceWindowState } from './workspace';
import type { Conversation } from '$lib/types/api';

describe('workspace activity states', () => {
  it.each([
    ['running', { active: true, attention: false, error: false }],
    ['ready', { active: true, attention: false, error: false }],
    ['paused', { active: false, attention: true, error: false }],
    ['failed', { active: false, attention: false, error: true }],
    ['completed', { active: false, attention: false, error: false }],
    ['enabled', { active: false, attention: false, error: false }],
  ])('maps %s without treating enabled schedules as running', (status, expected) => {
    expect(entityActivityState(status)).toMatchObject(expected);
  });

  it('surfaces unread conversation activity only while minimized', () => {
    const window = {
      kind: 'conversation',
      entityId: 'conversation-1',
      minimized: false,
    } as WorkspaceWindowState;
    const conversation = {
      conversation_id: 'conversation-1',
      has_unread: true,
      has_active_turn: false,
      pending_notification_types: [],
      context: {},
    } as unknown as Conversation;
    expect(workspaceWindowActivityState(window, conversation).unread).toBe(false);
    expect(workspaceWindowActivityState({ ...window, minimized: true }, conversation).unread).toBe(true);
  });

  it('uses canonical resolved-conversation metadata for minimized agent windows', () => {
    const window = {
      kind: 'agent',
      entityId: 'agent-1',
      resolvedConversationId: 'conversation-1',
      title: 'Agent title',
      minimized: true,
    } as WorkspaceWindowState;
    const conversation = {
      conversation_id: 'conversation-1',
      title: 'Canonical conversation title',
      has_unread: true,
      has_active_turn: true,
      pending_notification_types: ['question'],
      context: {},
    } as unknown as Conversation;

    expect(workspaceConversationForWindow(window, [conversation])).toBe(conversation);
    expect(workspaceWindowTitle(window, conversation)).toBe('Canonical conversation title');
    expect(workspaceWindowActivityState(window, conversation)).toMatchObject({
      active: false,
      attention: true,
      unread: false,
      label: 'Waiting for input',
    });
  });
});
