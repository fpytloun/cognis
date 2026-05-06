<script lang="ts">
  import { goto } from '$app/navigation';

  let { error, status } = $props();

  function message(): string {
    return error instanceof Error ? error.message : 'Unexpected application error';
  }

  async function goHome(): Promise<void> {
    await goto('/');
  }
</script>

<svelte:head>
  <title>Application Error · Cognis</title>
</svelte:head>

<div class="app-fullscreen-safe flex items-center justify-center overflow-y-auto">
  <div class="w-full max-w-xl rounded-3xl border border-rose-500/30 bg-slate-900/90 p-8 shadow-card backdrop-blur">
    <p class="text-sm font-medium uppercase tracking-[0.3em] text-rose-300">Error {status}</p>
    <h1 class="mt-3 text-3xl font-semibold text-white">Something went wrong</h1>
    <p class="mt-4 text-sm leading-6 text-slate-300">{message()}</p>
    <div class="mt-8 flex flex-wrap gap-3">
      <button
        class="rounded-xl bg-sky-500 px-4 py-2 text-sm font-medium text-slate-950 transition hover:bg-sky-400"
        onclick={goHome}
      >
        Return home
      </button>
    </div>
  </div>
</div>
