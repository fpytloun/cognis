<script lang="ts">
  let { files, additions = 0, deletions = 0, compact = false }: {
    files?: number | null;
    additions?: number | null;
    deletions?: number | null;
    compact?: boolean;
  } = $props();
</script>

<span class="inline-flex flex-wrap items-center gap-1.5 font-mono text-[10px]" data-testid="diff-stat">
  {#if files !== undefined && files !== null}
    <span class="text-slate-300">{files} {files === 1 ? 'file' : 'files'}</span>
  {/if}
  {#if additions}<span class="text-emerald-300" aria-label={`${additions} additions`}>+{additions}</span>{/if}
  {#if deletions}<span class="text-rose-300" aria-label={`${deletions} deletions`}>−{deletions}</span>{/if}
  {#if !compact && files == null && !additions && !deletions}<span class="text-slate-500">No file changes</span>{/if}
</span>
