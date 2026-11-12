<script lang="ts">
  import { userPreferences } from '$lib/stores/userPreferences';
  import type { Conversation } from '$lib/types/api';
  import ProjectAvatar from './ProjectAvatar.svelte';
  import ProjectGlyph from './ProjectGlyph.svelte';
  let { conversation, selected = false, onToggle } = $props<{
    conversation: Pick<Conversation, 'project_id' | 'project_name' | 'project_color' | 'project_avatar_url' | 'project_icon'>; selected?: boolean; onToggle?: () => void;
  }>();
  const mode = $derived($userPreferences.display.conversation_project_badge ?? 'name');
  const color = $derived(/^#[0-9a-f]{6}$/i.test(conversation.project_color ?? '')
    ? conversation.project_color : '#38bdf8');
</script>

{#if conversation.project_name && mode !== 'hidden'}
  <button
    type="button" disabled={!onToggle}
    onclick={(event) => { event.stopPropagation(); onToggle?.(); }}
    onkeydown={(event) => event.stopPropagation()}
    aria-pressed={selected}
    class="inline-flex max-w-[3.5rem] items-center text-[9px] font-medium leading-[14px]"
    class:rounded={mode === 'name'} class:px-1={mode === 'name'}
    style:color={mode === 'monochrome' ? 'rgb(var(--color-muted))' : color} style:background-color={mode === 'name' ? `${color}18` : 'transparent'}
    title={`${selected ? 'Clear' : 'Filter by'} project: ${conversation.project_name}`}
    aria-label={`Project: ${conversation.project_name}`}
    data-testid="conversation-project-badge"
  >
    {#if mode === 'icon'}
      <ProjectAvatar name={conversation.project_name} avatarUrl={conversation.project_avatar_url} icon={conversation.project_icon} color={conversation.project_color} class="h-4 w-4" />
    {:else if mode === 'monochrome'}
      <ProjectGlyph icon={conversation.project_icon ?? 'briefcase'} class="h-4 w-4" />
    {/if}
    {#if mode === 'name'}<span class="truncate">{conversation.project_name}</span>{/if}
  </button>
{/if}
