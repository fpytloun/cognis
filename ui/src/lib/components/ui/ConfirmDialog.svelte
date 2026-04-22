<script lang="ts">
  import { confirmStore, resolveConfirm } from '$lib/stores/confirm';
  import Button from '$lib/components/ui/Button.svelte';
  import BlockingDialog from '$lib/components/ui/BlockingDialog.svelte';
</script>

{#if $confirmStore}
  <BlockingDialog
    label="Confirmation dialog"
    onClose={() => resolveConfirm(false)}
    titleId="confirm-title"
  >
    {#snippet header()}
      <div>
        <p class="text-sm uppercase tracking-[0.25em] text-slate-400">Confirmation required</p>
        <h2 class="mt-3 text-xl font-semibold text-white" id="confirm-title">{$confirmStore.title}</h2>
      </div>
    {/snippet}

    {#snippet children()}
      <p class="text-sm leading-6 text-slate-300">{$confirmStore.message}</p>
    {/snippet}

    {#snippet footer()}
      <div class="flex flex-wrap justify-end gap-3">
        <Button variant="secondary" onclick={() => resolveConfirm(false)}>{$confirmStore.cancelLabel}</Button>
        <Button variant={$confirmStore.variant === 'primary' ? 'primary' : 'danger'} onclick={() => resolveConfirm(true)}>{$confirmStore.confirmLabel}</Button>
      </div>
    {/snippet}
  </BlockingDialog>
{/if}
