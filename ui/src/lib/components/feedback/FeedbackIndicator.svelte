<script lang="ts">
  import { onMount } from 'svelte';
  import { getFeedbackSurface } from '$lib/feedback/context';
  import CommentControl from './CommentControl.svelte';
  let { count, onOpen, variant = 'full', idle = 'button' }: {
    count?: number; onOpen?: () => void; variant?: 'full' | 'compact' | 'message'; idle?: 'button' | 'hidden';
  } = $props();
  const surface = getFeedbackSurface();
  const visible = $derived(count ?? (surface?.commentCount !== undefined
    ? surface.commentCount ?? 0 : surface?.binding?.highlights.length ?? 0));
  const denied = $derived(!!surface?.binding && !surface.binding.source.capabilities.comment);
  const disabled = $derived(denied && !surface?.requestCommentAccess);
  const id = Symbol('feedback indicator');
  onMount(() => surface?.registerIndicator?.(id));
</script>
{#if (!surface?.registerIndicator || surface.indicatorOwner === id) && (onOpen || (surface?.available ?? Boolean(surface?.binding)))}
  {#if variant === 'compact' || variant === 'message'}
    {#if visible || idle === 'button'}
      <button type="button" class={variant === 'message' ? 'copy-icon-button relative' : 'feedback-icon relative text-muted hover:text-accent'} title="Comment on this message"
        aria-label={visible ? `Comment on this message (${visible} comments)` : 'Comment on this message'}
        {disabled}
        onclick={() => denied && surface?.requestCommentAccess ? surface.requestCommentAccess() : surface?.whole ? surface.whole() : (onOpen ?? surface?.open)?.()}>
        <svg class="h-4 w-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" aria-hidden="true"><path d="M21 15a3 3 0 0 1-3 3H8l-5 4V6a3 3 0 0 1 3-3h12a3 3 0 0 1 3 3zM12 7v7m-3.5-3.5h7" stroke-linecap="round" stroke-linejoin="round" /></svg>
        {#if visible}<span class={variant === 'message' ? 'absolute -right-0.5 -top-0.5 rounded-full bg-surface px-0.5 text-[8px] leading-3' : 'ml-1 text-[10px]'}>{visible}</span>{/if}
      </button>
    {/if}
  {:else if !surface?.workspaceOwned}
    <CommentControl open={!!surface?.railOpen} count={visible} {disabled}
      onShow={() => denied && surface?.requestCommentAccess ? surface.requestCommentAccess() : onOpen ? onOpen() : surface?.show ? surface.show() : surface?.open()} />
  {/if}
{/if}
