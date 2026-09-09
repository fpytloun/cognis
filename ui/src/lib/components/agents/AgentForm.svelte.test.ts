import { fireEvent, render, screen, within } from '@testing-library/svelte';
import { describe, expect, it, vi } from 'vitest';
import { createReactiveAgentForm } from './agentFormTestState.svelte';
import AgentForm from './AgentForm.svelte';

function setup(readonly = false) {
  const form = createReactiveAgentForm();
  form.name = 'Research assistant';
  const onSave = vi.fn();
  const view = render(AgentForm, {
    mode: 'create', form, tools: [{
      name: 'read', description: 'Read a file', category: 'filesystem', parameters: {},
      read_only: true, capabilities: [], source: { type: 'builtin' },
      timeout_seconds: 30, non_bypassable: false
    }], workflows: [], providers: [], readonly, onSave
  });
  return { ...view, onSave };
}

describe('AgentForm sections', () => {
  it('shows identity first and preserves edits across sections', async () => {
    const { container } = setup();
    const name = screen.getByPlaceholderText('Research Assistant');
    await fireEvent.input(name, { target: { value: 'Changed name' } });
    await fireEvent.click(screen.getByRole('tab', { name: 'Providers & models' }));
    expect(name).not.toBeVisible();
    expect(within(screen.getByRole('tabpanel')).getByText('Memory backend')).toBeVisible();
    await fireEvent.click(screen.getByRole('tab', { name: 'Identity' }));
    expect(name).toHaveValue('Changed name');
    expect(container.querySelectorAll('[role="tabpanel"]:not([hidden])')).toHaveLength(1);
  });

  it('keeps a single save action available in every section', async () => {
    const { onSave } = setup();
    await fireEvent.click(screen.getByRole('tab', { name: 'Tools & access 1' }));
    await fireEvent.click(screen.getByRole('button', { name: 'Create agent' }));
    expect(onSave).toHaveBeenCalledOnce();
    expect(onSave.mock.calls[0][0].name).toBe('Research assistant');
  });

  it('links validation feedback to the affected section', async () => {
    setup();
    await fireEvent.input(screen.getByPlaceholderText('Research Assistant'), { target: { value: '' } });
    await fireEvent.click(screen.getByRole('tab', { name: 'Providers & models' }));
    await fireEvent.click(screen.getByRole('button', { name: 'Name is required.' }));
    expect(screen.getByRole('tab', { name: 'Identity !' })).toHaveAttribute('aria-selected', 'true');
    expect(screen.getByRole('button', { name: 'Create agent' })).toBeDisabled();
  });

  it('does not expose a save action for a shared read-only agent', () => {
    setup(true);
    expect(screen.queryByRole('button', { name: 'Create agent' })).not.toBeInTheDocument();
    expect(screen.getByPlaceholderText('Research Assistant')).toBeDisabled();
  });

  it('prevents search Enter from submitting and retains filtered selections', async () => {
    const { onSave } = setup();
    await fireEvent.click(screen.getByRole('tab', { name: 'Tools & access 1' }));
    const search = screen.getByRole('searchbox');
    await fireEvent.input(search, { target: { value: 'read' } });
    await fireEvent.click(screen.getByRole('checkbox', { name: 'read' }));
    const enter = new KeyboardEvent('keydown', { key: 'Enter', bubbles: true, cancelable: true });
    search.dispatchEvent(enter);
    expect(enter.defaultPrevented).toBe(true);
    expect(onSave).not.toHaveBeenCalled();
    await fireEvent.input(search, { target: { value: 'absent tool' } });
    expect(screen.getByRole('status')).toHaveTextContent('No tools match');
    await fireEvent.input(search, { target: { value: 'read' } });
    expect(screen.getByRole('checkbox', { name: 'read' })).not.toBeChecked();
    await fireEvent.click(screen.getByRole('tab', { name: 'Identity' }));
    await fireEvent.click(screen.getByRole('button', { name: 'Create agent' }));
    expect(onSave.mock.calls[0][0].tools.disabled_tools).toEqual(['read']);
  });
});
