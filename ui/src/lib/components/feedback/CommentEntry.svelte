<script lang="ts">
  import type { Snippet } from 'svelte';
  import type { FeedbackEntryVersion } from '$lib/feedback/types';
  import { formatAbsoluteTime, formatRelativeTime } from '$lib/time';
  import AgentAvatar from '$lib/components/AgentAvatar.svelte';
  /** One comment as every comment list shows it: avatar, author, time and body. */
  let { entry, clamp = false, body = true, aside }: { entry: FeedbackEntryVersion; clamp?: boolean; body?: boolean; aside?: Snippet } = $props();
</script>
<span class="flex min-w-0 flex-1 items-start gap-2 text-left">
  <span class="shrink-0" aria-hidden="true"><AgentAvatar name={entry.author.label} class="h-7 w-7 rounded-lg text-xs" /></span>
  <span class="min-w-0 flex-1">
    <span class="flex min-w-0 items-center gap-1.5 text-xs text-muted">
      <span class="truncate font-medium text-foreground">{entry.author.label}</span>
      {#if entry.author.kind === 'agent'}<span class="text-accent">Agent</span>{/if}
      {#if entry.author.kind === 'guest_unverified'}<span>Guest</span>{/if}
      <span aria-hidden="true">·</span>
      <time class="shrink-0" datetime={entry.created_at} title={formatAbsoluteTime(entry.created_at)}>{formatRelativeTime(entry.created_at)}</time>
      {#if entry.version > 1}<span>· Edited</span>{/if}
      {#if aside}<span class="ml-auto shrink-0">{@render aside()}</span>{/if}
    </span>
    {#if body}<span class="block whitespace-pre-wrap break-words text-sm text-foreground" class:line-clamp-3={clamp}>{entry.body}</span>{/if}
  </span>
</span>
