<script lang="ts">
  import Info from 'lucide-svelte/icons/info';

  let { open, loading = false, ariaControls, testId, onclick } = $props<{
    open: boolean;
    loading?: boolean;
    ariaControls: string;
    testId: string;
    onclick: (event: MouseEvent) => void;
  }>();
</script>

<button
  class="touch-target-compact inline-flex items-center justify-center rounded-lg border border-slate-700 text-slate-400 transition hover:bg-slate-800 hover:text-slate-100 disabled:opacity-60"
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
