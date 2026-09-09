import type { AttentionActionDetail, AttentionActionSummary } from '$lib/types/api';

export function isAttentionActionQuickAction(
  action: AttentionActionSummary,
): boolean {
  return (
    action.status === 'pending'
    && action.availability === 'actionable'
    && action.can_resolve === true
    && action.has_action_form === true
  );
}

export function isAttentionActionDetailActionable(
  action: AttentionActionDetail,
): boolean {
  return (
    isAttentionActionQuickAction(action)
    && action.allowed_actions.some((choice) => (
      Boolean(choice.action.trim())
      && Boolean(choice.label.trim())
      && (
        choice.input !== 'structured'
        || action.display.questions.length > 0
      )
      && (
        !['credential', 'auth_fields'].includes(choice.input)
        || action.display.required_fields.length > 0
      )
    ))
  );
}
