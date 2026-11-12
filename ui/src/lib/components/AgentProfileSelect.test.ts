import { cleanup, fireEvent, render, screen } from '@testing-library/svelte';
import { afterEach, expect, it } from 'vitest';
import AgentProfileSelect from './AgentProfileSelect.svelte';
import type { Agent } from '$lib/types/api';

afterEach(cleanup);

it('preserves a persisted profile while agent metadata is unavailable', () => {
  render(AgentProfileSelect, { agentId: 'reviewer', value: 'careful', agents: [] });
  expect(screen.getByRole('combobox')).toHaveValue('careful');
  expect(screen.getByRole('combobox')).toBeDisabled();
});

function agent(enabled = true): Agent {
  return {
    agent_id: 'engineer', name: 'Engineer', agent_type: 'primary', status: 'active',
    execution: { auto_profile_selection: { enabled, min_confidence: 0.85 } },
    agent_profiles: {
      routine: { profile_id: 'routine', description: 'Routine work.', enabled: true },
      auto: { profile_id: 'auto', description: 'An explicitly named profile.', enabled: true, agent_switchable: false },
    },
    default_agent_profile_id: 'routine',
    owner_email: 'owner@test.local',
    display_name: null,
    description: null,
    system_prompt: null,
    personality: null,
    skills: null,
    tools: null,
    permissions: null,
    llm_config: null,
    personality_synced: false,
    personality_sync_error: null,
    personality_sync_checked_at: null,
    avatar_url: null,
    avatar_image_id: null,
    is_system: false,
    hidden: false,
    editable_fields: [],
    has_overrides: false,
    disabled: false,
    disableable: true,
    sync_metadata: null,
    is_shared_with_me: false,
    shared_by_email: null,
    granted_permission: null,
    executor_scope: null,
    is_readonly_for_caller: false,
    created_at: null,
    updated_at: null,
  };
}

it('defaults new chats to Auto and keeps policy values separate from real profile IDs', async () => {
  render(AgentProfileSelect, { agents: [agent()], agentId: 'engineer', allowAuto: true });
  const select = screen.getByRole('combobox');
  expect(select).toHaveValue('mode:auto');
  expect(screen.getByRole('option', { name: /auto · select once/i })).toBeInTheDocument();
  // A human can still explicitly choose a manual-only profile whose actual ID is "auto".
  await fireEvent.change(select, { target: { value: 'profile:auto' } });
  expect(select).toHaveValue('profile:auto');
  await fireEvent.change(select, { target: { value: 'mode:default' } });
  expect(select).toHaveValue('mode:default');
});

it('honors agent opt-out and does not offer Auto when disabled', () => {
  render(AgentProfileSelect, { agents: [agent(false)], agentId: 'engineer', allowAuto: true });
  expect(screen.getByRole('combobox')).toHaveValue('mode:default');
  expect(screen.queryByRole('option', { name: /auto · select once/i })).not.toBeInTheDocument();
});

it('preserves the explicit-only interface used by task, workflow and channel settings', () => {
  render(AgentProfileSelect, { agents: [agent()], agentId: 'engineer', value: 'auto' });
  expect(screen.getByRole('combobox')).toHaveValue('auto');
  expect(screen.queryByRole('option', { name: /select once/i })).not.toBeInTheDocument();
});
