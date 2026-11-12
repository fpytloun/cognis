import { cleanup, fireEvent, render, screen } from '@testing-library/svelte';
import { afterEach, expect, it, vi } from 'vitest';
import EscalationPrompt from './EscalationPrompt.svelte';
import type { Escalation } from '$lib/types/api';

afterEach(cleanup);
const item: Escalation = {
  call_id: 'approval-staging', tool_name: 'deploy_service', decision: 'escalate',
  resolved: false, session_id: null, reasoning: 'Deploy to staging', risk: 'Service restart',
  arguments_display: { environment: 'staging' },
};

it('collapses without resolving or losing the note and preserves expiry and queue status', async () => {
  const onApprove = vi.fn();
  const onDeny = vi.fn();
  render(EscalationPrompt, { item, secondsRemaining: 42, queuedCount: 2, onApprove, onDeny });
  const toggle = screen.getByRole('button', { name: 'Approval required · deploy_service' });
  const note = screen.getByRole('textbox', { name: 'Note (optional)' });
  await fireEvent.input(note, { target: { value: 'Staging only' } });
  await fireEvent.click(toggle);
  expect(toggle).toHaveAttribute('aria-expanded', 'false');
  expect(screen.queryByRole('button', { name: 'Approve' })).toBeNull();
  expect(screen.getByText('42s left')).toBeVisible();
  expect(screen.getByText('+2 queued')).toBeVisible();
  expect(onApprove).not.toHaveBeenCalled();
  expect(onDeny).not.toHaveBeenCalled();
  await fireEvent.click(toggle);
  expect(screen.getByRole('textbox', { name: 'Note (optional)' })).toHaveValue('Staging only');
  await fireEvent.click(screen.getByRole('button', { name: 'Approve' }));
  expect(onApprove).toHaveBeenCalledWith('Staging only');
});

it('reopens and clears the note for a different approval but not a countdown refresh', async () => {
  const props = { item, secondsRemaining: 42, onApprove: vi.fn(), onDeny: vi.fn() };
  const { rerender } = render(EscalationPrompt, props);
  await fireEvent.input(screen.getByRole('textbox'), { target: { value: 'Review note' } });
  await fireEvent.click(screen.getByRole('button', { name: /Approval required/ }));
  await rerender({ ...props, secondsRemaining: 41 });
  expect(screen.getByRole('button', { name: /Approval required/ })).toHaveAttribute('aria-expanded', 'false');
  await rerender({ ...props, item: { ...item, call_id: 'approval-next' } });
  expect(screen.getByRole('button', { name: /Approval required/ })).toHaveAttribute('aria-expanded', 'true');
  expect(screen.getByRole('textbox')).toHaveValue('');
});

it('retains notes after a failed decision and prevents expired approvals', async () => {
  const onApprove = vi.fn().mockResolvedValue(false);
  const props = { item, secondsRemaining: 42, onApprove, onDeny: vi.fn() };
  const { rerender } = render(EscalationPrompt, props);
  await fireEvent.input(screen.getByRole('textbox'), { target: { value: 'Retry note' } });
  await fireEvent.click(screen.getByRole('button', { name: 'Approve' }));
  expect(screen.getByRole('textbox')).toHaveValue('Retry note');
  await rerender({ ...props, secondsRemaining: 0 });
  expect(screen.getByRole('button', { name: 'Approve' })).toBeDisabled();
  expect(screen.getByRole('button', { name: 'Deny' })).toBeDisabled();
});
