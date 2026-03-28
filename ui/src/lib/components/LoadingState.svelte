<script lang="ts">
  let { label = 'Loading…', description = 'Please wait while Cognis prepares this view.' } = $props<{
    label?: string;
    description?: string;
  }>();
</script>

<div class="flex min-h-[280px] flex-col items-center justify-center gap-4 rounded-3xl border border-slate-800/80 bg-slate-900/80 px-6 py-12 text-center shadow-card backdrop-blur">
  <div class="h-10 w-10 animate-spin rounded-full border-2 border-slate-700 border-t-sky-400"></div>
  <div class="space-y-2">
    <h2 class="text-lg font-semibold text-white">{label}</h2>
    <p class="max-w-md text-sm leading-6 text-slate-400">{description}</p>
  </div>
</div>
