<script lang="ts">
  import '../rich/document-actions.css';
  import Button from '$lib/components/ui/Button.svelte';
  /**
   * The one comment button of an opened artifact or deliverable. It opens the comment list (with
   * comment mode on) and closes it again; the count is what waits on the reviewer.
   */
  let { open = false, count = 0, onShow, disabled = false }: {
    open?: boolean; count?: number; onShow: () => void; disabled?: boolean;
  } = $props();
  const label = $derived(open ? 'Hide comments' : count ? `Comments, ${count} need you` : 'Comments');
</script>
<Button variant="ghost" size="icon" class={`document-icon-action comment-toggle relative${open ? ' active' : ''}`} aria-pressed={open}
  aria-label={label} title={open ? 'Hide comments' : 'Comments: select text to comment'} {disabled} onclick={onShow}>
  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" aria-hidden="true"><path d="M21 11.5a8.4 8.4 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.4 8.4 0 0 1-3.8-.9L3 21l1.9-5.7a8.4 8.4 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.4 8.4 0 0 1 3.8-.9h.5a8.5 8.5 0 0 1 8 8v.5Z" stroke-linecap="round" stroke-linejoin="round" /></svg>
  {#if count}<span class="count" aria-hidden="true">{count > 99 ? '99+' : count}</span>{/if}
</Button>
<style>
  :global(.comment-toggle.active) { color: var(--rich-accent, rgb(var(--color-accent))); background: var(--rich-control-active, rgb(var(--color-surface-hover))); }
  .count { position: absolute; right: -4px; top: -4px; min-width: 1rem; height: 1rem; padding: 0 .25rem; border-radius: 9999px;
    background: var(--rich-accent, rgb(var(--color-accent))); color: rgb(var(--color-on-accent)); font-size: .625rem; line-height: 1rem; font-weight: 600; text-align: center; }
</style>
