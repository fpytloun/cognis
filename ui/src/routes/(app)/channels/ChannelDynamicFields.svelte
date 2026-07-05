<script lang="ts">
  import Input from '$lib/components/ui/Input.svelte';
  import type { ChannelEditorDraft } from '$lib/channels';
  import type { ChannelMeta } from '$lib/types/api';

  export let meta: ChannelMeta;
  export let draft: ChannelEditorDraft;
  export let editing = false;
  export let credentialOverrides: Record<string, string> = {};

  function optionLabel(fieldName: string, option: string): string {
    if (fieldName === 'assistant_delivery_mode') {
      if (option === 'final_only') return 'Final';
      if (option === 'concatenated') return 'Concatenated';
      if (option === 'immediate') return 'Immediate';
    }
    if (fieldName === 'dm_conversation_mode' || fieldName === 'group_conversation_mode') {
      if (option === 'default') return 'Default (continue chat, fork threads)';
      if (option === 'threads') return 'Threads (one thread per top-level message)';
    }
    if (fieldName === 'thread_start_mode') {
      if (option === 'fork') return 'Fork from source turn';
      if (option === 'fresh') return 'Fresh session with root context';
    }
    return option;
  }
</script>

{#each meta.credential_fields as field}
  <label class="grid gap-2 text-sm text-slate-300">
    {field.label}
    <Input
      value={editing
        ? (field.secret ? (credentialOverrides[field.name] ?? '') : (draft.credentialValues[field.name] ?? ''))
        : (draft.credentialValues[field.name] ?? '')}
      oninput={(event) => {
        const value = (event.currentTarget as HTMLInputElement).value;
        if (editing && field.secret) {
          credentialOverrides[field.name] = value;
        } else {
          draft.credentialValues[field.name] = value;
        }
      }}
      type={field.secret ? 'password' : 'text'}
      placeholder={editing ? 'Configured (enter new value to replace)' : (field.description || field.label)}
    />
    {#if field.description}
      <span class="text-xs text-slate-500">{field.description}</span>
    {/if}
  </label>
{/each}

{#each meta.setting_fields as field}
  <label class="grid gap-2 text-sm text-slate-300">
    {field.label}
    {#if field.field_type === 'select' && field.options}
      <select bind:value={draft.settingValues[field.name]} class="w-full rounded-xl border border-slate-700 bg-slate-950/80 px-3 py-2 text-sm text-slate-100">
        {#each field.options as option}
          <option value={option}>{optionLabel(field.name, option)}</option>
        {/each}
      </select>
    {:else if field.field_type === 'boolean'}
      <select bind:value={draft.settingValues[field.name]} class="w-full rounded-xl border border-slate-700 bg-slate-950/80 px-3 py-2 text-sm text-slate-100">
        <option value="true">Enabled</option>
        <option value="false">Disabled</option>
      </select>
    {:else}
      <Input bind:value={draft.settingValues[field.name]} placeholder={field.description || field.label} />
    {/if}
    {#if field.description}
      <span class="text-xs text-slate-500">{field.description}</span>
    {/if}
  </label>
{/each}
