import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import { afterEach, describe, expect, it } from 'vitest';
import TaskFinishInstructions from './TaskFinishInstructions.svelte';

afterEach(cleanup);

describe('Task finish instructions', () => {
  it('offers bounded instructions without presuming a notification-only outcome', async () => {
    render(TaskFinishInstructions, {
      deliveryMode: 'preferred_channel', completionMode: 'default', instructions: '',
    });
    const input = screen.getByRole('textbox', { name: /Finish instructions/ });
    await fireEvent.input(input, { target: { value: 'On failure, inspect and verify one authorized repair.' } });
    expect(input).toHaveValue('On failure, inspect and verify one authorized repair.');
    expect(input).toHaveAttribute('maxlength', '8000');
    expect(screen.getByText(/Existing permissions still apply/)).toBeVisible();
  });

  it('makes inbox delivery direct-only and does not imply finish instructions will run', async () => {
    render(TaskFinishInstructions, {
      deliveryMode: 'in_app_notifications', completionMode: 'default', instructions: 'Inspect failure',
    });
    await waitFor(() => expect(screen.getByRole('textbox')).toBeDisabled());
    expect(screen.getByText(/without an agent turn/)).toBeVisible();
    expect(screen.getByText(/Finish instructions do not run/)).toBeVisible();
    expect(screen.getByText(/Select Agent/)).toBeVisible();
  });

  it('allows hybrid instructions but explains that they do not run on success', () => {
    render(TaskFinishInstructions, {
      deliveryMode: 'preferred_channel', completionMode: 'auto', instructions: 'Inspect failures',
    });
    expect(screen.getByRole('textbox')).toBeEnabled();
    expect(screen.getByText(/uses these instructions only for non-success outcomes/)).toBeVisible();
  });
});
