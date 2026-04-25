<script lang="ts">
  let { status } = $props<{ status: string }>();

  function normalizedStatus(): string {
    return status.trim().toLowerCase();
  }

  function tone(): string {
    const normalized = normalizedStatus();
    return normalized === 'healthy' || normalized === 'active'
      ? 'border-emerald-500/40 bg-emerald-500/10 text-emerald-100'
      : normalized === 'degraded'
        ? 'border-sky-500/40 bg-sky-500/10 text-sky-100'
        : normalized === 'disabled' || normalized === 'unknown'
          ? 'border-slate-700 bg-slate-900/80 text-slate-300'
          : 'border-rose-500/40 bg-rose-500/10 text-rose-100';
  }
</script>

<span class={`inline-flex items-center rounded-full border px-3 py-1 text-xs font-semibold uppercase tracking-[0.2em] ${tone()}`}>
  {status}
</span>
