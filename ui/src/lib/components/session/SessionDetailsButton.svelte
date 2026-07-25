<script lang="ts">
  import Info from 'lucide-svelte/icons/info';

  let { open, loading = false, onclick } = $props<{
    open: boolean;
    loading?: boolean;
    onclick: () => void;
  }>();
</script>

<button
  class="inline-flex h-9 w-9 items-center justify-center rounded-lg border border-slate-700 text-slate-400 transition hover:bg-slate-800 hover:text-slate-100 disabled:opacity-60 sm:h-8 sm:w-8"
  {onclick}
  type="button"
  title="Session details"
  aria-label="Toggle session details"
  aria-expanded={open}
  disabled={loading}
>
  <Info class={`h-4 w-4 ${loading ? 'animate-pulse' : ''}`} />
</button>
