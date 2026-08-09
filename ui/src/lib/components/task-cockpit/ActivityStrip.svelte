<script lang="ts">
  import LoaderCircle from 'lucide-svelte/icons/loader-circle';

  let { label, stepName = '' } = $props<{ label: string; stepName?: string }>();
</script>

<section class="flex items-center gap-3 rounded-2xl border border-sky-500/20 bg-sky-500/5 px-4 py-3" aria-label="Current activity" data-testid="task-activity">
  <LoaderCircle class="h-5 w-5 shrink-0 animate-spin text-sky-300 motion-reduce:animate-none" />
  <div class="min-w-0">
    <p class="text-xs font-semibold uppercase tracking-[0.2em] text-sky-300">Activity</p>
    <p class="truncate text-sm text-slate-200">{label}{#if stepName} · <span class="text-white">{stepName}</span>{/if}</p>
  </div>
</section>
