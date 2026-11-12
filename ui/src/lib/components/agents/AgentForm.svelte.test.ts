import { fireEvent, render, screen, within, waitFor } from '@testing-library/svelte';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { agentToFormState } from '$lib/agents';
import { createReactiveAgentForm } from './agentFormTestState.svelte';
import AgentForm from './AgentForm.svelte';
import { api } from '$lib/api/client';
import type { LLMProvider } from '$lib/types/api';

function setup(readonly = false, agents: any[] = [], providers: LLMProvider[] = []) {
  const form = createReactiveAgentForm();
  form.name = 'Research assistant';
  const onSave = vi.fn();
  const view = render(AgentForm, {
    mode: 'create', form, tools: [{
      name: 'read', description: 'Read a file', category: 'filesystem', parameters: {},
      read_only: true, capabilities: [], source: { type: 'builtin' },
      timeout_seconds: 30, non_bypassable: false
    }], workflows: [], providers, agents, readonly, onSave
  });
  return { ...view, form, onSave };
}

describe('AgentForm sections', () => {
  afterEach(() => vi.restoreAllMocks());
  it('keeps execution settings but removes automatic workflow selection controls', async () => {
    const { onSave } = setup();
    await fireEvent.click(screen.getByRole('tab', { name: 'Workflows' }));
    expect(screen.getByRole('heading', { name: 'Execution settings' })).toBeVisible();
    expect(screen.queryByRole('heading', { name: 'Available workflows' })).not.toBeInTheDocument();
    expect(screen.queryByRole('combobox', { name: 'Default workflow' })).not.toBeInTheDocument();
    expect(screen.queryByRole('combobox', { name: 'Selection mode' })).not.toBeInTheDocument();
    await fireEvent.click(screen.getByRole('button', { name: 'Create agent' }));
    const execution = onSave.mock.calls[0][0].execution;
    for (const key of ['available_workflow_ids', 'default_workflow_id', 'workflow_selection_mode']) {
      expect(execution).not.toHaveProperty(key);
    }
    expect(execution).toHaveProperty('step_agent_overrides');
  });
  const routeResult = { routes: [{
    route_id: 'chat-route', reference: 'cognis/chat', revision: 1,
    config: { name: 'chat', targets: [{ provider_id: 'anthropic', model: 'claude', priority: 0 }] }
  }] };
  function setupExistingRoute() {
    const form = createReactiveAgentForm();
    Object.assign(form, agentToFormState({
      agent_id: 'research', name: 'Research', agent_type: 'primary',
      llm_config: { model: 'cognis/chat', reasoning_effort: 'high' },
      agent_profiles: { default: { profile_id: 'default', reasoning_effort: 'high' } }
    } as never));
    const onSave = vi.fn();
    render(AgentForm, {
      form, mode: 'edit', tools: [], workflows: [], agents: [], dirty: true,
      providers: [{
        provider_id: 'anthropic', config: {}, display_name: 'Anthropic',
        models: [{ model_id: 'claude', reasoning_efforts: ['default', 'high'] }]
      } as LLMProvider], onSave
    });
    return { form, onSave };
  }
  it('loads existing and inherited profile routes on Identity before validating saved efforts', async () => {
    let resolve!: (value: typeof routeResult) => void;
    const list = vi.spyOn(api.inferenceRoutes, 'list').mockReturnValue(
      new Promise(done => { resolve = done; })
    );
    const { form, onSave } = setupExistingRoute();
    await waitFor(() => expect(list).toHaveBeenCalledTimes(1));
    expect(screen.getByRole('status')).toHaveTextContent('Loading route capabilities');
    expect(screen.queryByText(/Thinking effort.*unavailable/)).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Save changes' })).toBeDisabled();
    resolve(routeResult);
    await waitFor(() => expect(screen.getByRole('button', { name: 'Save changes' })).toBeEnabled());
    await fireEvent.click(screen.getByRole('button', { name: 'Save changes' }));
    expect(form.agentProfiles[0].reasoningEffort).toBe('high');
    expect(onSave.mock.calls[0][0].agent_profiles.default.reasoning_effort).toBe('high');
  });
  it('offers explicit retry without discarding saved values on discovery failure', async () => {
    const list = vi.spyOn(api.inferenceRoutes, 'list')
      .mockRejectedValueOnce(new Error('offline')).mockResolvedValueOnce(routeResult);
    const { form } = setupExistingRoute();
    await screen.findByRole('button', { name: 'Retry route discovery' });
    expect(screen.queryByText(/Thinking effort.*unavailable/)).not.toBeInTheDocument();
    expect(form.reasoningEffort).toBe('high');
    await fireEvent.click(screen.getByRole('button', { name: 'Retry route discovery' }));
    await waitFor(() => expect(screen.getByRole('button', { name: 'Save changes' })).toBeEnabled());
    expect(list).toHaveBeenCalledTimes(2);
  });
  it('still rejects genuinely unsupported saved efforts after discovery succeeds', async () => {
    vi.spyOn(api.inferenceRoutes, 'list').mockResolvedValue(routeResult);
    const { form } = setupExistingRoute();
    form.agentProfiles[0].reasoningEffort = 'max';
    await screen.findByText('Thinking effort for profile "default" is unavailable. Select an available value.');
    expect(screen.getByRole('button', { name: 'Save changes' })).toBeDisabled();
  });
  it('groups memory and guardrails while keeping configured advanced controls and warnings visible', async () => {
    const { container, form, onSave } = setup();
    form.maximumOutcome = 'approve';
    await fireEvent.click(screen.getByRole('tab', { name: 'Providers & models' }));
    expect(screen.getByRole('heading', { name: 'Memory' })).toBeVisible();
    expect(screen.getByRole('heading', { name: 'Guardrails' })).toBeVisible();
    const advanced = container.querySelector('details:has(select)')!;
    expect(advanced).toHaveAttribute('open');
    expect(screen.getByRole('combobox', { name: /^Maximum evaluation outcome/ })).toHaveValue('approve');
    await fireEvent.click(within(advanced as HTMLElement).getByText('Advanced evaluation controls'));
    expect(advanced).not.toHaveAttribute('open');
    expect(screen.getByText('DANGEROUS — Yolo mode:').closest('[role="alert"]')).toBeVisible();
    await fireEvent.click(screen.getByRole('button', { name: 'Create agent' }));
    expect(onSave).toHaveBeenCalledWith(expect.objectContaining({
      capabilities: expect.objectContaining({ maximum_outcome: 'approve' })
    }));
    await fireEvent.change(screen.getByRole('combobox', { name: /^Guardrails backend/ }), { target: { value: 'none' } });
    expect(screen.queryByRole('combobox', { name: /^Intaris evaluation/ })).not.toBeInTheDocument();
    await fireEvent.click(screen.getByRole('button', { name: 'Create agent' }));
    expect(form.maximumOutcome).toBe('approve');
  });
  it('keeps analytics editable without guardrails and serializes false', async () => {
    const { onSave } = setup();
    await fireEvent.click(screen.getByRole('tab', { name: 'Providers & models' }));
    const analytics = screen.getByRole('checkbox', { name: /Session analytics/ });
    expect(analytics).toBeChecked();
    await fireEvent.change(screen.getByRole('combobox', { name: /^Guardrails backend/ }), { target: { value: 'none' } });
    expect(analytics).toBeEnabled();
    await fireEvent.click(analytics);
    await fireEvent.click(screen.getByRole('button', { name: 'Create agent' }));
    expect(onSave.mock.calls[0][0].capabilities).toMatchObject({
      analytics_enabled: false, guardrails_backend: 'none'
    });
  });
  it('hides guardrail-only controls under None and restores draft values when re-enabled', async () => {
    const { form, onSave } = setup();
    form.intarisEvaluation = 'strict';
    form.approvalMode = 'unattended';
    form.minimumOutcome = 'escalate';
    form.maximumOutcome = 'approve';
    form.escalationTimeoutSeconds = '120';
    form.toolPermissions = { read: 'deny' };
    await fireEvent.click(screen.getByRole('tab', { name: 'Providers & models' }));
    const backend = screen.getByRole('combobox', { name: /^Guardrails backend/ });
    expect(screen.getByRole('combobox', { name: /^Intaris evaluation/ })).toHaveValue('strict');
    expect(screen.getByRole('combobox', { name: /^Approvals/ })).toHaveValue('unattended');
    expect(screen.getByRole('combobox', { name: /^Minimum evaluation outcome/ })).toHaveValue('escalate');
    expect(screen.getByRole('combobox', { name: /^Maximum evaluation outcome/ })).toHaveValue('approve');
    expect(screen.getByRole('spinbutton', { name: /^Escalation timeout/ })).toHaveValue(120);
    await fireEvent.change(backend, { target: { value: 'none' } });
    for (const name of [/^Intaris evaluation/, /^Approvals/, /^Minimum evaluation outcome/, /^Maximum evaluation outcome/]) {
      expect(screen.queryByRole('combobox', { name })).not.toBeInTheDocument();
    }
    expect(screen.queryByText('Advanced evaluation controls')).not.toBeInTheDocument();
    expect(screen.queryByRole('spinbutton', { name: /^Escalation timeout/ })).not.toBeInTheDocument();
    expect(screen.queryByText('DANGEROUS — Yolo mode:')).not.toBeInTheDocument();
    const analytics = screen.getByRole('checkbox', { name: /Session analytics/ });
    expect(analytics).toBeEnabled();
    await fireEvent.click(analytics);
    await fireEvent.click(screen.getByRole('button', { name: 'Create agent' }));
    const disabled = onSave.mock.calls[0][0];
    expect(disabled.capabilities).toMatchObject({ guardrails_backend: 'none', analytics_enabled: false });
    for (const key of ['minimum_outcome', 'maximum_outcome', 'escalation_timeout_seconds']) {
      expect(disabled.capabilities).not.toHaveProperty(key);
    }
    expect(disabled.permissions.tool_permissions).toEqual({ read: 'deny' });
    await fireEvent.change(backend, { target: { value: 'intaris' } });
    expect(screen.getByRole('combobox', { name: /^Intaris evaluation/ })).toHaveValue('strict');
    expect(screen.getByRole('combobox', { name: /^Approvals/ })).toHaveValue('unattended');
    expect(screen.getByText('Advanced evaluation controls')).toBeVisible();
    expect(screen.getByRole('combobox', { name: /^Minimum evaluation outcome/ })).toHaveValue('escalate');
    expect(screen.getByRole('combobox', { name: /^Maximum evaluation outcome/ })).toHaveValue('approve');
    expect(screen.getByRole('spinbutton', { name: /^Escalation timeout/ })).toHaveValue(120);
    expect(analytics).not.toBeChecked();
    await fireEvent.click(screen.getByRole('button', { name: 'Create agent' }));
    expect(onSave.mock.calls[1][0].capabilities).toMatchObject({
      guardrails_backend: 'intaris', analytics_enabled: false, intaris_evaluation: 'strict',
      approval_mode: 'unattended', minimum_outcome: 'escalate', maximum_outcome: 'approve',
      escalation_timeout_seconds: 120
    });
  });
  it('starts a saved None agent with only backend and analytics controls in Guardrails', async () => {
    const form = createReactiveAgentForm();
    Object.assign(form, agentToFormState({
      agent_id: 'no-guardrails', name: 'Storage and analytics',
      capabilities: { guardrails_backend: 'none', analytics_enabled: false }
    } as never));
    render(AgentForm, { mode: 'edit', form, tools: [], workflows: [], agents: [], providers: [], onSave: vi.fn() });
    await fireEvent.click(screen.getByRole('tab', { name: 'Providers & models' }));
    const card = screen.getByRole('heading', { name: /^Guardrails$/ }).parentElement!;
    expect(within(card).getAllByRole('combobox')).toHaveLength(1);
    expect(within(card).getByRole('combobox', { name: /^Guardrails backend/ })).toHaveValue('none');
    expect(within(card).getByRole('checkbox', { name: /Session analytics/ })).not.toBeChecked();
    expect(within(card).getByRole('checkbox', { name: /Session analytics/ })).toBeEnabled();
    expect(within(card).queryByText('Advanced evaluation controls')).not.toBeInTheDocument();
  });
  it('offers Cognis routes for both agent defaults and runtime profiles', async () => {
    const list = vi.spyOn(api.inferenceRoutes, 'list').mockResolvedValue({
      routes: [{ route_id: 'chat-route', reference: 'cognis/chat', revision: 1,
        config: { name: 'chat', targets: [{ provider_id: 'anthropic', model: 'claude', priority: 0 }] } }]
    });
    const { form, onSave } = setup(false, [], [{
      provider_id: 'anthropic', display_name: 'Anthropic', config: {},
      models: [{ model_id: 'claude', reasoning_efforts: ['default', 'low', 'high'], supports_fast_mode: true }]
    } as LLMProvider]);
    await fireEvent.click(screen.getByRole('tab', { name: 'Providers & models' }));
    const provider = screen.getByRole('combobox', { name: /^Provider/ });
    await fireEvent.change(provider, { target: { value: 'cognis' } });
    await screen.findByRole('option', { name: 'cognis/chat' });
    await fireEvent.change(screen.getByRole('combobox', { name: /^Model/ }), {
      target: { value: 'cognis/chat' }
    });
    await fireEvent.change(screen.getByRole('combobox', { name: /Thinking effort/ }), { target: { value: 'high' } });
    const fast = screen.getByRole('combobox', { name: /^Fast mode/ });
    expect(within(fast).getByRole('option', { name: 'Enabled' })).not.toBeDisabled();
    await fireEvent.change(fast, { target: { value: 'enabled' } });
    await fireEvent.click(screen.getByRole('tab', { name: /^Profiles/ }));
    await fireEvent.click(screen.getByRole('button', { name: /Add profile/ }));
    await fireEvent.change(screen.getByRole('combobox', { name: /^Provider/ }), {
      target: { value: 'cognis' }
    });
    await fireEvent.change(screen.getByRole('combobox', { name: /^Model/ }), {
      target: { value: 'cognis/chat' }
    });
    await fireEvent.change(screen.getByRole('combobox', { name: /Thinking effort/ }), { target: { value: 'low' } });
    await fireEvent.change(screen.getByRole('combobox', { name: /^Fast mode/ }), { target: { value: 'enabled' } });
    await fireEvent.click(screen.getByRole('button', { name: 'Create agent' }));
    const payload = onSave.mock.calls[0][0];
    expect(payload.llm_config).toMatchObject({ model: 'cognis/chat', reasoning_effort: 'high', fast_mode: true });
    expect(payload.llm_config.provider_id).toBeUndefined();
    expect(payload.agent_profiles[form.agentProfiles[0].profileId]).toMatchObject({
      model: 'cognis/chat', provider_id: null, reasoning_effort: 'low', fast_mode: true
    });
    list.mockRestore();
  });
  it('distinguishes inheritance from provider default without invented effort options', async () => {
    const { onSave } = setup();
    await fireEvent.click(screen.getByRole('tab', { name: 'Providers & models' }));
    const select = screen.getByRole('combobox', { name: /Thinking effort/ });
    expect(within(select).getByRole('option', { name: 'Inherit' })).toHaveValue('');
    expect(within(select).getByRole('option', { name: 'Provider default' })).toHaveValue('default');
    expect(within(select).queryByRole('option', { name: 'High' })).not.toBeInTheDocument();
    await fireEvent.change(select, { target: { value: 'default' } });
    await fireEvent.click(screen.getByRole('button', { name: 'Create agent' }));
    expect(onSave.mock.calls[0][0].llm_config.reasoning_effort).toBe('default');
    await fireEvent.change(select, { target: { value: '' } });
    await fireEvent.click(screen.getByRole('button', { name: 'Create agent' }));
    expect(onSave.mock.calls[1][0].llm_config.reasoning_effort).toBeUndefined();
  });

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
    await fireEvent.click(screen.getByRole('tab', { name: 'Tools 1' }));
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
    await fireEvent.click(screen.getByRole('tab', { name: 'Tools 1' }));
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

  it('uses agent pickers for delegation and keeps self implicitly allowed', async () => {
    const { form } = setup(false, [
      { agent_id: 'worker', name: 'Worker', agent_type: 'primary', status: 'active' },
      { agent_id: 'lumi', name: 'Lumi', agent_type: 'primary', status: 'active' },
      { agent_id: 'system:explore', name: 'Explore', agent_type: 'secondary', is_system: true, status: 'active' }
    ]);
    form.agentId = 'worker';
    await fireEvent.click(screen.getByRole('tab', { name: 'Access & delegation' }));
    expect(screen.getByText('Execution access')).toBeVisible();
    expect(screen.queryByRole('searchbox')).not.toBeInTheDocument();
    await fireEvent.click(screen.getByRole('checkbox', { name: 'Restrict allowed target agents' }));

    expect(screen.getByText('worker')).toBeVisible();
    expect(screen.getByText('always allowed')).toBeVisible();
    await fireEvent.click(screen.getByRole('checkbox', { name: /Lumi/ }));
    await fireEvent.click(screen.getByRole('checkbox', { name: /Explore/ }));
    expect(form.allowedAgentIds).toEqual(['lumi', 'system:explore']);
    await fireEvent.click(screen.getByRole('checkbox', { name: 'Restrict agents allowed to target this primary agent' }));
    const lumiChoices = screen.getAllByRole('checkbox', { name: /Lumi/ });
    await fireEvent.click(lumiChoices[lumiChoices.length - 1]);
    expect(form.allowedControllerAgentIds).toEqual(['lumi']);
  });
});
