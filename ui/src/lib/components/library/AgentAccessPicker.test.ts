import { fireEvent, render, screen } from '@testing-library/svelte';
import { describe, expect, it } from 'vitest';
import AgentAccessPicker from './AgentAccessPicker.svelte';

describe('agent avatar selection', () => {
  it('marks owner/inherited access and toggles selectable recipients', async () => {
    render(AgentAccessPicker, { ownerId: 'owner', selected: [], agents: [
      { agent_id: 'owner', name: 'Riker', inherited: true, explicit: false, avatar_url: '/api/v1/images/riker' },
      { agent_id: 'recipient', name: 'Mira', inherited: false, explicit: false },
      { agent_id: 'project', name: 'LaForge', inherited: true, explicit: false },
    ] });
    expect(screen.getByRole('button', { name: 'Share with Riker' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Share with LaForge' })).toBeDisabled();
    const selectable = screen.getByRole('button', { name: 'Share with Mira' });
    expect(selectable).toHaveAttribute('aria-pressed', 'false');
    await fireEvent.click(selectable);
    expect(selectable).toHaveAttribute('aria-pressed', 'true');
    await fireEvent.click(selectable);
    expect(selectable).toHaveAttribute('aria-pressed', 'false');
    expect(screen.getAllByRole('img').length).toBeGreaterThan(0);
  });
  it('removes a redundant explicit grant without falsely removing inherited access', async () => {
    render(AgentAccessPicker, { selected: ['project'], agents: [
      { agent_id: 'project', name: 'LaForge', inherited: true, explicit: true },
    ] });
    const target = screen.getByRole('button', { name: 'Share with LaForge' });
    expect(target).not.toBeDisabled();
    await fireEvent.click(target);
    expect(target).toBeDisabled();
    expect(target).toHaveAttribute('aria-pressed', 'true');
    expect(screen.getByText('Inherited access')).toBeInTheDocument();
  });
  it('edits explicit permissions independently of inherited project write', async () => {
    render(AgentAccessPicker, { permissions: true, selected: ['project'], writers: ['project'], agents: [
      { agent_id: 'project', name: 'LaForge', inherited: true, explicit: true,
        explicit_permission: 'write', inherited_permission: 'write', inherited_sources: ['project'] },
    ] });
    expect(screen.getByText('Project · read + write')).toBeVisible();
    const permission = screen.getByRole('combobox', { name: 'Explicit permission for LaForge' });
    expect(permission).toHaveValue('write');
    await fireEvent.change(permission, { target: { value: 'read' } });
    expect(permission).toHaveValue('read');
    const target = screen.getByRole('button', { name: 'Share with LaForge' });
    await fireEvent.click(target);
    expect(target).not.toBeDisabled();
    expect(target).toHaveAttribute('aria-pressed', 'false');
    expect(screen.getByText('Project · read + write')).toBeVisible();
    expect(screen.queryByRole('combobox')).not.toBeInTheDocument();
  });
});
