import { fireEvent, render, screen } from '@testing-library/svelte';
import { expect, it, vi } from 'vitest';

import type { AttentionActionDetail } from '$lib/types/api';
import AttentionActionRenderer from './AttentionActionRenderer.svelte';

function credentialAction(): AttentionActionDetail {
  return {
    action_id: 'credential-1',
    kind: 'credential_request',
    status: 'pending',
    availability: 'actionable',
    title: 'Credential required',
    source: {
      notification_id: 'credential-1',
      conversation_id: 'conversation-1',
      managed_origin_conversation_id: null,
      task_id: null,
      step_name: null,
      step_run_id: null,
      session_id: null,
    },
    can_resolve: true,
    has_action_form: true,
    expires_at: null,
    revision: 1,
    convergence_id: 'credential-1:1',
    display: {
      message: 'Enter the token.',
      tool_name: null,
      arguments_display: null,
      reasoning: null,
      risk: null,
      questions: [],
      required_fields: ['token'],
      credential_id: 'service-token',
      credential_kind: 'token',
      credential_label: 'Service token',
      credential_scope: 'user',
      authorization_url: null,
      user_code: null,
      callback_mode: null,
      executor_name: null,
    },
    allowed_actions: [
      { action: 'approve', label: 'Save and resume', intent: 'primary', input: 'credential' },
      { action: 'cancel', label: 'Cancel request', intent: 'secondary', input: 'none' },
    ],
  };
}

it('renders credential fields without network behavior and submits only entered values', async () => {
  const onSubmit = vi.fn();
  render(AttentionActionRenderer, { action: credentialAction(), onSubmit });

  const submit = screen.getByRole('button', { name: 'Save and resume' });
  expect(submit).toBeDisabled();
  await fireEvent.input(screen.getByLabelText('Token'), { target: { value: 'secret-value' } });
  expect(submit).toBeEnabled();
  await fireEvent.click(submit);

  expect(onSubmit).toHaveBeenCalledWith({
    action: 'approve',
    response_fields: { token: 'secret-value' },
  });
});
