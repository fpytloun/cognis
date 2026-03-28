<script lang="ts">
  let { status } = $props<{ status: string }>();

  function tone(): string {
    return status === 'healthy'
      ? 'border-emerald-500/40 bg-emerald-500/10 text-emerald-100'
      : status === 'degraded'
        ? 'border-amber-500/40 bg-amber-500/10 text-amber-100'
        : 'border-rose-500/40 bg-rose-500/10 text-rose-100';
  }
</script>

<span class={`inline-flex items-center rounded-full border px-3 py-1 text-xs font-semibold uppercase tracking-[0.2em] ${tone()}`}>
  {status}
</span>
