<script lang="ts">
  let { error } = $props();

  function message(): string {
    return error instanceof Error ? error.message : 'Unable to render this page';
  }
</script>

<section class="mx-auto max-w-4xl px-6 py-16">
  <div class="rounded-3xl border border-sky-400/30 bg-slate-900/90 p-8 shadow-card backdrop-blur">
    <p class="text-sm font-medium uppercase tracking-[0.3em] text-sky-300">Page Error</p>
    <h1 class="mt-3 text-2xl font-semibold text-white">This section could not be loaded</h1>
    <p class="mt-4 text-sm leading-6 text-slate-300">{message()}</p>
  </div>
</section>
