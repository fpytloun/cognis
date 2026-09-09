<script lang="ts">
  /**
   * Accessible text-only per-node execution status badge. The activity avatar
   * is the single visual progress indicator, so the badge never duplicates
   * motion.
   */
  let { label, active = false }: { label: string; active?: boolean } = $props();
</script>

<span
  class="inline-flex shrink-0 items-center gap-1 rounded-full bg-slate-800/70 px-1.5 py-0.5 text-[10px]"
  role={active ? 'status' : undefined}
  aria-busy={active ? 'true' : undefined}
  aria-live={active ? 'polite' : undefined}
  data-testid="workstream-execution-status"
>
  {label}
</span>
