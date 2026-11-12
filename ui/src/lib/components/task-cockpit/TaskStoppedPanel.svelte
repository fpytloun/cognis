<script lang="ts">
  import Button from '$lib/components/ui/Button.svelte';
  import type { TaskStoppedSummary } from '$lib/task-attention';

  let {
    summary,
    actionLabel = null,
    busy = false,
    onAction
  }: {
    summary: TaskStoppedSummary;
    actionLabel?: string | null;
    busy?: boolean;
    onAction?: () => void;
  } = $props();
</script>

<section
  class={`rounded-2xl border p-4 ${summary.tone === 'danger' ? 'border-danger-border bg-danger-subtle' : 'border-warning-border bg-warning-subtle'}`}
  aria-labelledby="task-stopped-title"
  data-testid="task-stopped"
>
  <div class="flex flex-wrap items-start justify-between gap-3">
    <div class="min-w-0 flex-1">
      <h2 id="task-stopped-title" class={`text-sm font-semibold ${summary.tone === 'danger' ? 'text-danger' : 'text-warning'}`}>{summary.title}</h2>
      <p class="mt-1 text-sm text-foreground">{summary.reason}</p>
      {#if summary.detail}
        <details class="mt-2 text-xs text-muted">
          <summary class="cursor-pointer">Technical detail</summary>
          <p class="mt-1 break-words font-mono">{summary.detail}</p>
        </details>
      {/if}
    </div>
    {#if actionLabel && onAction}
      <Button size="sm" disabled={busy} onclick={onAction}>{actionLabel}</Button>
    {/if}
  </div>
</section>
