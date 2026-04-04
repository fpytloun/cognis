<script lang="ts">
  import Link2 from 'lucide-svelte/icons/link-2';

  import Card from '$lib/components/ui/Card.svelte';
  import type { SetupGuide } from '$lib/channels';

  export let guide: SetupGuide | null = null;
  export let docsUrl: string | null = null;
</script>

{#if guide}
  <Card class="p-5">
    <div class="flex items-start justify-between gap-3">
      <div>
        <h3 class="text-lg font-semibold text-white">{guide.title}</h3>
        <p class="mt-1 text-sm text-slate-400">{guide.service}</p>
      </div>
      {#if docsUrl}
        <a class="inline-flex items-center gap-2 text-sm text-sky-300 hover:text-sky-200" href={docsUrl} target="_blank" rel="noreferrer">
          <Link2 class="h-4 w-4" /> Docs
        </a>
      {/if}
    </div>

    <div class="mt-4 rounded-2xl border border-slate-800 bg-slate-950/60 p-4">
      <p class="text-xs uppercase tracking-[0.24em] text-slate-500">Manual setup</p>
      <ol class="mt-3 space-y-2 text-sm text-slate-300">
        {#each guide.steps as step, index}
          <li class="flex gap-3"><span class="text-sky-300">{index + 1}.</span><span>{step}</span></li>
        {/each}
      </ol>
      <p class="mt-3 text-xs text-slate-500">
        {#if guide.publicUrlNeeded}
          This adapter needs a public webhook URL. Save the account first, then copy the generated webhook URL from Cognis.
        {:else}
          This adapter does not require a public webhook URL for the default setup.
        {/if}
      </p>
    </div>
  </Card>
{/if}
