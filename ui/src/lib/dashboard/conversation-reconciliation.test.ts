import { describe, expect, it } from 'vitest';

import type { AttentionActionSummary, Conversation } from '$lib/types/api';
import {
  AttentionActionTombstones,
  mergeReconciledConversation,
  reconcileConversationProjection,
} from './conversation-reconciliation';

function conversation(
  id: string,
  actions?: AttentionActionSummary[],
): Conversation {
  return {
    conversation_id: id,
    title: id,
    status: 'active',
    attention_actions: actions,
  } as Conversation;
}

function action(
  id: string,
  revision: number,
  status: 'pending' | 'resolving' = 'pending',
): AttentionActionSummary {
  return {
    action_id: id,
    kind: 'escalation',
    status,
    availability: status === 'resolving' ? 'resolving' : 'actionable',
    title: 'Tool approval required',
    source: {
      notification_id: id,
      conversation_id: 'tail',
      managed_origin_conversation_id: null,
      task_id: null,
      step_name: null,
      step_run_id: null,
      session_id: null,
    },
    can_resolve: true,
    expires_at: null,
    revision,
    convergence_id: `${id}:${revision}`,
  };
}

describe('conversation reconciliation', () => {
  it('updates an existing paginated tail in place without duplicates or unrelated detail rows', () => {
    const current = [
      conversation('head'),
      conversation('tail', [action('old-action', 1)]),
      conversation('older-tail'),
    ];
    const result = reconcileConversationProjection(
      current,
      [conversation('head')],
      [
        conversation('tail', [action('new-action', 1)]),
        conversation('unrelated', [action('unrelated-action', 1)]),
      ],
    );

    expect(result.map((item) => item.conversation_id)).toEqual([
      'head',
      'tail',
      'older-tail',
    ]);
    expect(result[1].attention_actions?.map((item) => item.action_id)).toEqual([
      'new-action',
    ]);
    expect(new Set(result.map((item) => item.conversation_id)).size).toBe(result.length);
  });

  it('tombstones a detail removal and rejects a delayed resolving revision', () => {
    const tombstones = new AttentionActionTombstones();
    const current = [
      conversation('head'),
      conversation('tail', [action('resolved-action', 1)]),
    ];
    const result = reconcileConversationProjection(
      current,
      [conversation('head')],
      [conversation('tail', [])],
      tombstones,
    );
    const stale = reconcileConversationProjection(
      result,
      [conversation('head')],
      [conversation('tail', [action('resolved-action', 2, 'resolving')])],
      tombstones,
    );

    expect(result[1].attention_actions).toEqual([]);
    expect(stale[1].attention_actions).toEqual([]);
  });

  it('preserves a newer action revision and preserves actions when projection is absent', () => {
    const current = conversation('tail', [action('approval', 4)]);
    const stale = mergeReconciledConversation(
      current,
      conversation('tail', [action('approval', 3)]),
    );
    const absent = mergeReconciledConversation(
      current,
      conversation('tail'),
    );

    expect(stale.attention_actions?.[0].revision).toBe(4);
    expect(absent.attention_actions?.[0].revision).toBe(4);
  });

  it('tombstones a head removal against later head and append revisions', () => {
    const tombstones = new AttentionActionTombstones();
    const current = [conversation('head', [action('approval', 1)])];
    const removed = reconcileConversationProjection(
      current,
      [conversation('head', [])],
      [],
      tombstones,
    );
    const staleCanonical = reconcileConversationProjection(
      removed,
      [conversation('head', [action('approval', 2, 'resolving')])],
      [],
      tombstones,
    );
    const staleAppend = reconcileConversationProjection(
      [],
      [conversation('head', [action('approval', 2, 'resolving')])],
      [],
      tombstones,
    );

    expect(staleCanonical[0].attention_actions).toEqual([]);
    expect(staleAppend[0].attention_actions).toEqual([]);
  });

  it('removes an out-of-scope reconciled tail without reordering retained rows', () => {
    const current = [
      { ...conversation('head'), project_id: 'project-a' },
      { ...conversation('moved'), project_id: 'project-a' },
      { ...conversation('retained'), project_id: 'project-a' },
    ];
    const result = reconcileConversationProjection(
      current,
      [current[0]],
      [{ ...conversation('moved'), project_id: 'project-b' }],
      new AttentionActionTombstones(),
      (item) => item.project_id === 'project-a' && item.status === 'active',
    );

    expect(result.map((item) => item.conversation_id)).toEqual(['head', 'retained']);
  });

  it('allows a different action ID and preserves current actions when projection is omitted', () => {
    const tombstones = new AttentionActionTombstones();
    const removed = mergeReconciledConversation(
      conversation('tail', [action('old-action', 1)]),
      conversation('tail', []),
      tombstones,
    );
    const withNewAction = mergeReconciledConversation(
      removed,
      conversation('tail', [action('new-action', 1)]),
      tombstones,
    );
    const omitted = mergeReconciledConversation(
      withNewAction,
      conversation('tail'),
      tombstones,
    );

    expect(withNewAction.attention_actions?.map((item) => item.action_id)).toEqual([
      'new-action',
    ]);
    expect(omitted.attention_actions).toEqual(withNewAction.attention_actions);
  });

  it('supports permanent-conversation and controller-lifetime cleanup', () => {
    const tombstones = new AttentionActionTombstones();
    tombstones.add('conversation-1', 'action-1');
    tombstones.add('conversation-2', 'action-1');

    expect(tombstones.has('conversation-1', 'action-1')).toBe(true);
    expect(tombstones.has('conversation-2', 'action-1')).toBe(true);
    tombstones.dropConversation('conversation-2');
    expect(tombstones.has('conversation-2', 'action-1')).toBe(false);
    tombstones.clear();
    expect(tombstones.has('conversation-1', 'action-1')).toBe(false);
  });

  it('tombstones a complete detail while the known conversation is outside the visible scope', () => {
    const tombstones = new AttentionActionTombstones();
    const loaded = reconcileConversationProjection(
      [],
      [conversation('tail', [action('approval', 1)])],
      [],
      tombstones,
    );
    expect(loaded[0].attention_actions?.[0].action_id).toBe('approval');

    const switchedScope = reconcileConversationProjection(
      [],
      [conversation('other')],
      [conversation('tail', [])],
      tombstones,
    );
    expect(switchedScope.map((item) => item.conversation_id)).toEqual(['other']);

    const staleReturn = reconcileConversationProjection(
      [],
      [conversation('tail', [action('approval', 2, 'resolving')])],
      [],
      tombstones,
    );
    expect(staleReturn[0].attention_actions).toEqual([]);
  });

  it('does not tombstone a known hidden conversation when detail omits the projection', () => {
    const tombstones = new AttentionActionTombstones();
    reconcileConversationProjection(
      [],
      [conversation('tail', [action('approval', 1)])],
      [],
      tombstones,
    );
    reconcileConversationProjection(
      [],
      [conversation('other')],
      [conversation('tail')],
      tombstones,
    );

    const returned = reconcileConversationProjection(
      [],
      [conversation('tail', [action('approval', 2, 'resolving')])],
      [],
      tombstones,
    );
    expect(returned[0].attention_actions?.[0].revision).toBe(2);
  });
});
