<script lang="ts">
  import { projectIconForeground, safeProjectColor } from '$lib/projects/icons';
  import ProjectGlyph from './ProjectGlyph.svelte';
  let { name, avatarUrl = null, icon = null, color = null, class: className = 'h-12 w-12' } = $props<{
    name: string; avatarUrl?: string | null; icon?: unknown; color?: string | null; class?: string;
  }>();
  const background = $derived(safeProjectColor(color));
  let failedAvatar = $state<string | null>(null);
</script>

{#if !avatarUrl || failedAvatar === avatarUrl}
  <span class={`relative inline-flex shrink-0 items-center justify-center rounded-[22%] ${className}`}
    style:background-color={background} style:color={projectIconForeground(background)}
    role="img" aria-label={name} data-testid="project-library-icon">
    <ProjectGlyph icon={icon ?? 'briefcase'} class="h-[70%] w-[70%]" />
  </span>
{:else}
  <img src={avatarUrl} alt={name} class={`shrink-0 rounded-[22%] object-cover ${className}`}
    onerror={() => { failedAvatar = avatarUrl; }} />
{/if}
