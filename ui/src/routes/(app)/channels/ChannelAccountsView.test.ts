import { cleanup, fireEvent, render, screen, within } from '@testing-library/svelte';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { channelAccounts, channelMeta, channelTypes } from '../../../../e2e/messaging-channels-fixture';
import { createChannelDraft, getChannelGuide } from '$lib/channels';
import ChannelAccountsView from './ChannelAccountsView.svelte';
import ChannelAccountEditor from './ChannelAccountEditor.svelte';
import ChannelTypePicker from './ChannelTypePicker.svelte';

afterEach(cleanup);

describe('Messaging channels', () => {
  it('groups accounts by platform and only exposes Edit for the fixed app channel', async () => {
    const onEdit = vi.fn();
    render(ChannelAccountsView, {
      accounts: channelAccounts,
      metas: Object.fromEntries(channelTypes.map((meta) => [meta.channel_type, meta])),
      agentName: (id: string) => id,
      pendingCount: () => 0, onCreate: vi.fn(), onEdit, onToggle: vi.fn(), onDelete: vi.fn(),
    });
    const app = within(screen.getByRole('region', { name: 'Cognis channels' }));
    expect(app.getAllByRole('button', { name: 'Edit' })).toHaveLength(2);
    for (const name of ['Start', 'Stop', 'Delete', 'Open']) {
      expect(app.queryByRole('button', { name })).not.toBeInTheDocument();
    }
    expect(app.queryByText('long_poll')).not.toBeInTheDocument();
    expect(app.queryByText(/DM:|Groups:|New conversations:/)).not.toBeInTheDocument();
    expect(screen.getByRole('region', { name: 'Signal channels' })).toBeVisible();
    expect(screen.getByRole('region', { name: 'WhatsApp channels' })).toBeVisible();
    await fireEvent.click(app.getAllByRole('button', { name: 'Edit' })[0]);
    expect(onEdit).toHaveBeenCalledWith(channelAccounts[0]);
  });

  it('only shows the generic preference in the app editor and locks the sole channel', () => {
    const meta = channelMeta('web', 'Cognis app');
    render(ChannelAccountEditor, {
      mode: 'edit', selectedType: meta, channelTypes, onlyChannel: true,
      draft: createChannelDraft(meta, [], channelAccounts[2]), credentialOverrides: {},
      agents: [], executors: [], guide: getChannelGuide('web'), isDirty: false,
      onClose: vi.fn(), onSelectType: vi.fn(), onSave: vi.fn(),
    });
    const preferred = screen.getByRole('checkbox', { name: /preferred channel/ });
    expect(preferred).toBeChecked();
    expect(preferred).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Save preference' })).toBeDisabled();
    expect(screen.queryByRole('combobox')).not.toBeInTheDocument();
    expect(screen.queryByRole('textbox')).not.toBeInTheDocument();
    expect(screen.getByText(/Selected automatically/)).toBeVisible();
    expect(screen.queryByText(/preferred task delivery/)).not.toBeInTheDocument();
  });

  it('does not offer the app channel when creating an account', () => {
    render(ChannelTypePicker, { channelTypes, onSelect: vi.fn() });
    expect(screen.queryByRole('button', { name: 'Select Cognis app' })).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Select Signal' })).toBeVisible();
  });
});
