<script lang="ts">
  import Info from 'lucide-svelte/icons/info';

  let { open, loading = false, ariaControls, testId, onclick, compact = false } = $props<{
    open: boolean;
    loading?: boolean;
    ariaControls: string;
    testId: string;
    onclick: (event: MouseEvent) => void;
    compact?: boolean;
  }>();
</script>

<button
  class={`touch-target-compact inline-flex items-center justify-center rounded-lg border-control-border transition hover:bg-raised disabled:opacity-60 ${compact ? 'sm:border' : 'border'} ${open ? 'text-accent' : 'text-muted hover:text-foreground'}`}
  {onclick}
  type="button"
  title="Session details"
  aria-label="Toggle session details"
  aria-expanded={open}
  aria-controls={ariaControls}
  data-testid={testId}
>
  <Info class={`h-4 w-4 ${loading ? 'animate-pulse' : ''}`} />
</button>
