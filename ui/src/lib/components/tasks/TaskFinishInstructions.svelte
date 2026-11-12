<script lang="ts">
  import type { CompletionModeFamily } from '$lib/types/api';
  let {
    deliveryMode,
    completionMode = $bindable(),
    instructions = $bindable(''),
    disabled = false,
  } = $props<{
    deliveryMode: string;
    completionMode: CompletionModeFamily;
    instructions?: string;
    disabled?: boolean;
  }>();

  $effect(() => {
    if (deliveryMode === 'in_app_notifications') completionMode = 'direct';
  });
</script>

<label class="grid gap-2 text-sm text-secondary">
  Finish instructions (optional)
  <textarea
    bind:value={instructions}
    disabled={disabled || completionMode === 'direct'}
    maxlength="8000"
    rows="3"
    placeholder="What should the agent do after this task finishes?"
    class="w-full rounded-xl border border-control-border bg-canvas/80 px-3 py-2 text-sm text-foreground disabled:opacity-50"
  ></textarea>
  {#if completionMode === 'auto'}
    <span class="text-xs text-muted">Default delivers successful results directly. The agent uses these instructions only for non-success outcomes, including failure, cancellation, or attention. Existing permissions still apply.</span>
  {:else if completionMode === 'default'}
    <span class="text-xs text-muted">The agent considers the outcome, current context, and your preferences. You can specify bounded recovery or what to do on success, failure, or cancellation. Existing permissions still apply.</span>
  {:else}
    <span class="text-xs text-muted">Direct delivers the canonical result or a self-contained outcome without an agent turn. Finish instructions do not run. Select Agent for follow-up instructions on every outcome, or Default for non-success outcomes only.</span>
  {/if}
</label>
