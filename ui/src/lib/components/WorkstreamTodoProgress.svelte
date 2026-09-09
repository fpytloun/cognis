<script lang="ts">
  import Popover from '$lib/components/ui/Popover.svelte';

  let { progress } = $props<{
    progress: { total: number; completed: number; in_progress: number };
  }>();

  const fraction = $derived(
    progress.total > 0
      ? Math.max(0, Math.min(1, (progress.completed + 0.5 * progress.in_progress) / progress.total))
      : 0,
  );
  const percent = $derived(Math.round(fraction * 100));
  const label = $derived(
    `Todo progress: ${progress.completed} completed, ${progress.in_progress} in progress, ${progress.total} total`,
  );
</script>

{#if progress.total > 0}
  <Popover text={label} placement="top">
      <button
        type="button"
        class="inline-flex h-5 w-5 items-center justify-center rounded-full text-emerald-300 focus:outline-none focus-visible:ring-2 focus-visible:ring-emerald-300/60"
         aria-label={label}
         title={label}
         data-testid="workstream-todo-progress"
         data-progress={fraction}
         onclick={(event) => event.stopPropagation()}
         onkeydown={(event) => {
           if (event.key === 'Enter' || event.key === ' ') event.stopPropagation();
         }}
       >
        <svg class="h-4 w-4 -rotate-90" viewBox="0 0 20 20" aria-hidden="true">
          <circle cx="10" cy="10" r="7" fill="none" stroke="currentColor" stroke-opacity="0.2" stroke-width="2.5" />
          <circle
            cx="10" cy="10" r="7" fill="none" stroke="currentColor"
            stroke-linecap="round" stroke-width="2.5" pathLength="100"
            stroke-dasharray="100" stroke-dashoffset={100 - percent}
          />
        </svg>
      </button>
  </Popover>
{/if}
