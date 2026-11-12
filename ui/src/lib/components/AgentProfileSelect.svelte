<script lang="ts">
  import type { Agent } from '$lib/types/api';
  import { autoProfileSelectionForAgent, profileOptionsForAgent } from '$lib/agents';

  export let agents: Agent[] = [];
  export let agentId = '';
  export let value = '';
  export let allowAuto = false;
  export let mode: 'auto' | 'default' | null = null;
  export let label = 'Agent profile';
  export let help = 'Leave empty to use the selected agent default profile.';
  export let disabled = false;

  $: selectedAgent = agents.find((agent) => agent.agent_id === agentId) ?? null;
  $: profileOptions = profileOptionsForAgent(selectedAgent);
  $: autoEnabled = autoProfileSelectionForAgent(selectedAgent).enabled;
  $: effectiveMode = autoEnabled && mode !== 'default' ? 'auto' : 'default';
  // UI policy values must not collide with real profile IDs such as "auto".
  $: selection = allowAuto
    ? value ? `profile:${value}` : `mode:${effectiveMode}`
    : value;

  function changeSelection(event: Event): void {
    const next = (event.currentTarget as HTMLSelectElement).value;
    if (!allowAuto) {
      value = next;
    } else if (next.startsWith('profile:')) {
      value = next.slice('profile:'.length);
      mode = null;
    } else {
      value = '';
      mode = next === 'mode:auto' ? 'auto' : 'default';
    }
  }
</script>

<label class="block space-y-2 text-sm font-medium text-foreground">
  <span>{label}</span>
  <select
    value={selection}
    on:change={changeSelection}
    disabled={disabled || !selectedAgent || (!allowAuto && profileOptions.length === 0)}
    class="w-full rounded-xl border border-control-border bg-surface px-3 py-2 text-sm text-foreground focus:border-accent disabled:opacity-50"
  >
    {#if allowAuto && autoEnabled}
      <option value="mode:auto">Auto · select once from the first request</option>
    {/if}
    <option value={allowAuto ? 'mode:default' : ''}>Default profile</option>
    {#if value && !profileOptions.some((profile) => profile.profileId === value)}
      <option value={allowAuto ? `profile:${value}` : value}>{value}</option>
    {/if}
    {#each profileOptions as profile}
      <option value={allowAuto ? `profile:${profile.profileId}` : profile.profileId}>{profile.label}</option>
    {/each}
  </select>
  {#if help}
    <small class="block text-xs font-normal text-quiet">{help}</small>
  {/if}
</label>
