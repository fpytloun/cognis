<script lang="ts">
  let {
    label,
    value,
    detail
  } = $props<{
    label: string;
    value: number | null;
    detail: string;
  }>();

  const normalized = $derived(value == null ? null : Math.max(0, Math.min(100, value)));
  const tone = $derived(
    normalized == null
      ? 'bg-slate-700'
      : normalized >= 95
        ? 'bg-rose-400'
        : normalized >= 80
          ? 'bg-sky-400'
          : 'bg-emerald-400'
  );
</script>

<div class="space-y-2">
  <div class="flex items-baseline justify-between gap-3 text-sm">
    <span class="font-medium text-slate-200">{label}</span>
    <span class="text-right text-xs text-slate-400">{detail}</span>
  </div>
  {#if normalized == null}
    <div class="h-1.5 overflow-hidden rounded-full bg-slate-800" aria-hidden="true">
      <div class="h-full w-0"></div>
    </div>
  {:else}
    <div
      class="h-1.5 overflow-hidden rounded-full bg-slate-800"
      role="progressbar"
      aria-label={label}
      aria-valuemin="0"
      aria-valuemax="100"
      aria-valuenow={Math.round(normalized)}
    >
      <div class={`h-full rounded-full transition-[width] duration-300 ${tone}`} style={`width: ${normalized}%`}></div>
    </div>
  {/if}
</div>
