import { describe, expect, it } from 'vitest';

import type { AttentionActionSummary } from '$lib/types/api';
import { isAttentionActionQuickAction } from './actions';

const valid = {
  action_id: 'action-1',
  kind: 'escalation',
  status: 'pending',
  availability: 'actionable',
  title: 'Tool approval required',
  source: {
    notification_id: 'action-1',
    conversation_id: 'conversation-1',
    managed_origin_conversation_id: null,
    task_id: 'task-1',
    step_name: null,
    step_run_id: null,
    session_id: null,
  },
  can_resolve: true,
  has_action_form: true,
  expires_at: null,
  revision: 1,
  convergence_id: 'action-1:1',
} satisfies AttentionActionSummary;

describe('isAttentionActionQuickAction', () => {
  it('requires a current actionable server-owned form', () => {
    expect(isAttentionActionQuickAction(valid)).toBe(true);
    expect(isAttentionActionQuickAction({ ...valid, status: 'resolving' })).toBe(false);
    expect(isAttentionActionQuickAction({ ...valid, availability: 'read_only' })).toBe(false);
    expect(isAttentionActionQuickAction({ ...valid, can_resolve: false })).toBe(false);
    expect(isAttentionActionQuickAction({ ...valid, has_action_form: false })).toBe(false);
    expect(isAttentionActionQuickAction({ ...valid, has_action_form: undefined })).toBe(false);
  });
});
