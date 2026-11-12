<script lang="ts">
  import MessageCircle from 'lucide-svelte/icons/message-circle';
  import { auth } from '$lib/stores/auth';
  import { watchDocumentCommentCount } from '$lib/document-comment-counts';

  let { id }: { id: string } = $props();
  let count = $state<number>();
  let failed = $state(false);
  $effect(() => {
    const ref = id;
    count = undefined; failed = false;
    if (!$auth.user?.email || !/^(art|dlv)_[a-f0-9]{32}$/.test(ref)) return;
    return watchDocumentCommentCount(ref, (value, error) => { count = value; failed = error; });
  });
</script>

{#if count}
  <span class="inline-flex shrink-0 items-center gap-1 text-xs normal-case tracking-normal text-accent" title={`${count} open comment${count === 1 ? '' : 's'}`} data-testid="document-comment-count">
    <MessageCircle size={12} aria-hidden="true" />{count} open
  </span>
{:else if failed}
  <span class="text-xs normal-case tracking-normal text-quiet" title="Comment count unavailable" aria-label="Comment count unavailable">—</span>
{/if}
