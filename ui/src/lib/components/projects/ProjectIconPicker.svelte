<script lang="ts">
  import Button from '$lib/components/ui/Button.svelte';
  import Input from '$lib/components/ui/Input.svelte';
  import ProjectAvatar from './ProjectAvatar.svelte';

  let { value = $bindable(''), color, disabled = false } = $props<{
    value?: string; color?: string; disabled?: boolean;
  }>();
  let catalog = $state(import('$lib/projects/icon-catalog'));
  let query = $state('');
  let limit = $state(60);
  $effect(() => { query; limit = 60; });
</script>

<fieldset {disabled} class="mt-4 min-w-0">
  <legend class="mb-2 text-sm font-medium text-secondary">Project icon</legend>
  <p class="mb-3 text-xs text-quiet">Used without an avatar and for monochrome display. Save project settings to apply.</p>
  <div class="mb-3 flex items-center gap-3">
    <ProjectAvatar name="Selected project icon" icon={value || 'briefcase'} {color} class="h-10 w-10" />
    <Button variant="secondary" aria-pressed={!value} onclick={() => { value = ''; }}>Use default</Button>
  </div>
  <Input type="search" aria-label="Search project icons" placeholder="Search icons, e.g. coffee, tree, music" bind:value={query} {disabled} />
  {#await catalog}
    <p class="mt-3 text-sm text-muted" role="status">Loading icons…</p>
  {:then catalog}
    {@const matches = catalog.filterProjectIcons(query)}
    <p class="my-3 text-xs text-muted" role="status">{matches.length} {matches.length === 1 ? 'icon' : 'icons'} · Lucide + Cognis</p>
    <div class="grid grid-cols-3 gap-2 sm:grid-cols-6 lg:grid-cols-8">
      {#each matches.slice(0, limit) as option (option.id)}
        <Button variant="secondary"
          class={`h-full min-w-0 flex-col gap-2 px-2 py-3 ${value === option.id ? 'ring-2 ring-accent' : ''}`}
          aria-label={`Use ${option.label} icon`} aria-pressed={value === option.id}
          onclick={() => { value = option.id; }}>
          <option.component class="h-6 w-6 shrink-0" strokeWidth={1.8} />
          <span class="w-full break-words text-center text-xs">{option.label}</span>
        </Button>
      {/each}
    </div>
    {#if matches.length === 0}
      <p class="mt-3 text-sm text-muted">No icons match “{query}”. Try another name.</p>
    {:else if matches.length > limit}
      <Button variant="secondary" class="mt-3" onclick={() => { limit += 60; }}>Show more icons</Button>
    {/if}
  {:catch}
    <p class="mt-3 text-sm text-danger" role="alert">Could not load the icon library.</p>
    <Button variant="secondary" onclick={() => { catalog = import('$lib/projects/icon-catalog'); }}>Retry</Button>
  {/await}
</fieldset>
