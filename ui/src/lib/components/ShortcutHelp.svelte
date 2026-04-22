<script lang="ts">
  import Button from '$lib/components/ui/Button.svelte';
  import BlockingDialog from '$lib/components/ui/BlockingDialog.svelte';
  import { closeShortcutHelp, shortcutHelpOpen } from '$lib/shortcuts';

  const shortcuts = [
    ['/', 'Focus chat composer'],
    ['Ctrl/Cmd + N', 'Start a new conversation'],
    ['Escape', 'Cancel current turn, close dialog, or blur composer'],
    ['?', 'Open keyboard shortcut help']
  ] as const;
</script>

{#if $shortcutHelpOpen}
  <BlockingDialog label="Keyboard shortcuts" onClose={closeShortcutHelp} titleId="shortcut-help-title" panelClass="max-w-xl">
    {#snippet header()}
      <div class="flex items-start justify-between gap-3">
        <div>
          <p class="text-sm uppercase tracking-[0.25em] text-slate-400">Keyboard shortcuts</p>
          <h2 class="mt-2 text-xl font-semibold text-white" id="shortcut-help-title">Workspace shortcuts</h2>
        </div>
        <Button size="sm" variant="secondary" onclick={closeShortcutHelp}>Close</Button>
      </div>
    {/snippet}

    {#snippet children()}
      <div class="space-y-3">
        {#each shortcuts as [keys, description]}
          <div class="flex items-center justify-between gap-4 rounded-2xl border border-slate-800 bg-slate-950/70 px-4 py-3">
            <span class="rounded-lg border border-slate-700 bg-slate-900 px-3 py-1 text-xs font-semibold uppercase tracking-[0.2em] text-slate-200">{keys}</span>
            <span class="text-sm text-slate-300">{description}</span>
          </div>
        {/each}
      </div>
    {/snippet}
  </BlockingDialog>
{/if}
