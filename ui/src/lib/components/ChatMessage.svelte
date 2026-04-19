<script lang="ts">
  import Check from 'lucide-svelte/icons/check';
import Copy from 'lucide-svelte/icons/copy';
  import { onMount } from 'svelte';
  import type { MessageTimelineItem } from '$lib/chat';
  import AgentAvatar from '$lib/components/AgentAvatar.svelte';
  import AgentProfilePopover from '$lib/components/AgentProfilePopover.svelte';
  import LiveDots from '$lib/components/LiveDots.svelte';
  import { addToast } from '$lib/stores/toasts';
  import { now as nowStore } from '$lib/stores/now';
  import { formatAbsoluteTime, formatCompactTime } from '$lib/time';
  import type { Agent } from '$lib/types/api';

  let { item, agent = null } = $props<{
    item: MessageTimelineItem;
    agent?: Agent | null;
  }>();

  const agentName = $derived(
    agent ? (agent.display_name ?? agent.name) : 'Assistant'
  );
  const agentAvatarUrl = $derived(agent?.avatar_url ?? null);

  // Per-message agent profile popover state. Clicking the leading avatar
  // opens a popover with the agent's name, type, and description — the
  // same popover shown from the chat header — so the identity is always
  // reachable from within a conversation without scrolling to the top.
  let showAgentProfile = $state(false);

  // Subscribe to the single global "now" ticker instead of starting a per-message
  // setInterval. On conversations with dozens of visible messages this avoids
  // dozens of redundant timers and improves mobile battery life.
  const nowDate = $derived(new Date($nowStore));
  let messageCopied = $state(false);
  let copiedCodeBlocks = $state<Set<string>>(new Set());
  let copyResetTimer: number | null = null;
  const codeCopyResetTimers = new Map<string, number>();

  function sizeClass(): string {
    return item.role === 'assistant'
      ? 'min-w-0 flex-1 xl:max-w-3xl'
      : 'w-full min-w-0 sm:max-w-[88%] xl:max-w-2xl';
  }

  onMount(() => {
    return () => {
      if (copyResetTimer !== null) {
        window.clearTimeout(copyResetTimer);
      }
      codeCopyResetTimers.forEach((timer) => window.clearTimeout(timer));
      codeCopyResetTimers.clear();
    };
  });

  function bubbleClass(): string {
    return item.role === 'user'
      ? 'ml-auto bg-sky-500 text-slate-950'
      : item.role === 'assistant'
        ? 'bg-slate-900 text-slate-100 border border-slate-800'
        : 'bg-slate-800/70 text-slate-200 border border-slate-700';
  }

  function proseClass(): string {
    return item.role === 'user'
      ? 'prose-user prose-headings:text-slate-950 prose-p:text-slate-950 prose-strong:text-slate-950 prose-code:text-slate-800 prose-code:before:content-none prose-code:after:content-none prose-a:text-slate-900 prose-a:underline'
      : 'prose-invert prose-code:text-sky-200 prose-code:before:content-none prose-code:after:content-none';
  }

  function isImage(mimeType: string | undefined): boolean {
    return typeof mimeType === 'string' && mimeType.startsWith('image/');
  }

  async function copyMessage(): Promise<void> {
    try {
      await navigator.clipboard.writeText(item.content);
      messageCopied = true;
      if (copyResetTimer !== null) {
        window.clearTimeout(copyResetTimer);
      }
      copyResetTimer = window.setTimeout(() => {
        messageCopied = false;
        copyResetTimer = null;
      }, 2000);
    } catch {
      addToast('Failed to copy message', 'error');
    }
  }

  /**
   * Idempotent code-copy button mounter.
   *
   * Previously, this directive's `update` path ran on every streaming chunk,
   * tearing down and rebuilding every `<pre>`'s copy button. On a long
   * assistant reply with several code blocks that was the single most
   * expensive mobile work during streaming.
   *
   * Now:
   *   - Only `<pre>` elements that don't already carry our data-attribute
   *     receive a button. Existing mounts are left alone.
   *   - On each sync() we also cancel any timers for `<pre>` elements that
   *     vanished (e.g. streaming tail rebuilt by markdown streamer) so we
   *     don't keep trying to restore the label on a detached node.
   */
  function addCodeCopyButtons(
    node: HTMLDivElement,
    _html: string
  ): { update: (_html: string) => void; destroy: () => void } {
    // Track which pre element a copyKey currently maps to, so we can clear
    // timers for keys whose pre has been replaced.
    const labelByKey = new Map<string, HTMLElement>();

    const cancelTimer = (copyKey: string): void => {
      const existing = codeCopyResetTimers.get(copyKey);
      if (existing !== undefined) {
        window.clearTimeout(existing);
        codeCopyResetTimers.delete(copyKey);
      }
    };

    const markCopied = (copyKey: string, label: HTMLElement): void => {
      copiedCodeBlocks = new Set([...copiedCodeBlocks, copyKey]);
      label.textContent = 'Copied';
      cancelTimer(copyKey);
      const reset = window.setTimeout(() => {
        const next = new Set(copiedCodeBlocks);
        next.delete(copyKey);
        copiedCodeBlocks = next;
        codeCopyResetTimers.delete(copyKey);
        // Only touch the label if it's still in the DOM — if the streaming
        // rerender replaced the <pre>, we'd be writing to a detached node.
        const current = labelByKey.get(copyKey);
        if (current && current.isConnected) current.textContent = 'Copy';
      }, 2000);
      codeCopyResetTimers.set(copyKey, reset);
    };

    const sync = (): void => {
      const blocks = Array.from(node.querySelectorAll<HTMLElement>('pre'));
      const seenKeys = new Set<string>();

      for (const [index, block] of blocks.entries()) {
        const copyKey = `${item.id}:${index}`;
        seenKeys.add(copyKey);

        if (block.dataset.copyMounted === '1') continue;
        const code = block.querySelector('code');
        if (!code) continue;

        block.classList.add('chat-code-block');
        block.dataset.copyMounted = '1';

        const button = document.createElement('button');
        const label = document.createElement('span');
        label.textContent = copiedCodeBlocks.has(copyKey) ? 'Copied' : 'Copy';
        button.type = 'button';
        button.className = 'chat-code-copy-button';
        button.setAttribute('aria-label', 'Copy code block');
        button.innerHTML =
          '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="9" y="9" width="13" height="13" rx="2" ry="2"></rect><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"></path></svg>';
        button.append(label);

        const onClick = async (): Promise<void> => {
          try {
            await navigator.clipboard.writeText(code.textContent ?? '');
            markCopied(copyKey, label);
          } catch {
            addToast('Failed to copy code block', 'error');
          }
        };

        button.addEventListener('click', onClick);
        block.append(button);
        labelByKey.set(copyKey, label);
      }

      // Cancel timers for copyKeys whose <pre> has been removed by a
      // streaming tail rebuild, so we don't attempt to restore text on a
      // detached label.
      for (const copyKey of labelByKey.keys()) {
        if (!seenKeys.has(copyKey)) {
          cancelTimer(copyKey);
          labelByKey.delete(copyKey);
        }
      }
    };

    sync();

    return {
      update(_nextHtml: string) {
        // New <pre> elements may have been appended by the streaming tail
        // re-render. Existing ones are skipped.
        sync();
      },
      destroy() {
        for (const copyKey of labelByKey.keys()) cancelTimer(copyKey);
        labelByKey.clear();
      }
    };
  }
</script>

<!--
  Assistant messages are wrapped in a row: a 32px agent avatar on the
  leading edge and the bubble filling the remaining width. The footer
  inside the bubble repeats the agent name next to a small avatar so the
  identity stays visible at the end of long responses, where a reader
  has usually scrolled past the top-of-message avatar.

  User messages stay right-aligned without a leading avatar — the
  right-edge alignment already communicates "from me" and matches
  standard messaging-app conventions on mobile.
-->
{#if item.role === 'assistant'}
  <div class="flex w-full min-w-0 items-start gap-2 sm:gap-3">
    <div class="relative mt-0.5 shrink-0">
      {#if agent}
        <button
          type="button"
          class="block rounded-2xl transition hover:ring-2 hover:ring-sky-400/40 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sky-400"
          aria-label={`View ${agentName} profile`}
          onclick={() => { showAgentProfile = !showAgentProfile; }}
        >
          <AgentAvatar name={agentName} avatarUrl={agentAvatarUrl} class="h-8 w-8 text-xs" />
        </button>
        {#if showAgentProfile}
          <AgentProfilePopover {agent} onClose={() => { showAgentProfile = false; }} />
        {/if}
      {:else}
        <AgentAvatar name={agentName} avatarUrl={agentAvatarUrl} class="h-8 w-8 text-xs" />
      {/if}
    </div>
    <article class={`overflow-hidden rounded-[1.4rem] px-3 py-2.5 shadow-card sm:rounded-3xl sm:px-4 sm:py-3 ${sizeClass()} ${bubbleClass()}`}>
      {#if item.html}
        <div use:addCodeCopyButtons={item.html} class={`chat-markdown prose max-w-none overflow-x-auto break-words prose-pre:overflow-x-auto ${proseClass()}`}>{@html item.html}</div>
      {:else}
        <p class="whitespace-pre-wrap break-words text-sm leading-6 [overflow-wrap:anywhere]">{item.content}</p>
      {/if}

      {#if item.attachments && item.attachments.length > 0}
        <div class="mt-4 space-y-3">
          {#each item.attachments as attachment}
            {#if attachment.url && isImage(attachment.mime_type)}
              <a href={attachment.url} target="_blank" rel="noreferrer" class="block overflow-hidden rounded-2xl border border-slate-800 bg-slate-950/60">
                <img src={attachment.url} alt={attachment.filename} class="max-h-80 w-full object-cover" loading="lazy" />
                <div class="px-3 py-2 text-xs text-slate-400">{attachment.filename}</div>
              </a>
            {:else if attachment.url}
              <a href={attachment.url} target="_blank" rel="noreferrer" class="flex items-center justify-between rounded-2xl border border-slate-800 bg-slate-950/60 px-3 py-3 text-sm text-slate-200 hover:border-slate-600">
                <span class="truncate">{attachment.filename}</span>
                <span class="text-xs text-slate-500">{attachment.mime_type}</span>
              </a>
            {:else}
              <div class="rounded-2xl border border-slate-800 bg-slate-950/60 px-3 py-3 text-sm text-slate-300">{attachment.filename}</div>
            {/if}
          {/each}
        </div>
      {/if}

      <div class="mt-2.5 flex items-center justify-between gap-3 text-[11px] opacity-80 sm:mt-3">
        <div class="flex min-w-0 items-center gap-1.5">
          <AgentAvatar name={agentName} avatarUrl={agentAvatarUrl} class="h-4 w-4 rounded-md text-[9px]" />
          <span class="truncate font-medium text-slate-200">{agentName}</span>
          <span class="text-slate-500">·</span>
          <span class="text-slate-400" title={formatAbsoluteTime(item.timestamp)}>{formatCompactTime(item.timestamp, nowDate)}</span>
        </div>
        <div class="flex shrink-0 items-center gap-2">
          {#if item.streaming}
            <!-- The animated dots alone convey streaming; the "Live" label was
                 visual noise. An sr-only span keeps an accessible name. -->
            <LiveDots inline={true} size="sm" tone="sky" />
            <span class="sr-only">Streaming</span>
          {/if}
          {#if !item.streaming}
            <button
              class="inline-flex h-10 w-10 items-center justify-center rounded-full text-slate-400 transition hover:bg-slate-800/80 hover:text-slate-100 md:h-8 md:w-8"
              onclick={copyMessage}
              type="button"
              title="Copy raw markdown"
              aria-label="Copy raw markdown"
            >
              {#if messageCopied}
                <Check class="h-4 w-4 md:h-3.5 md:w-3.5" />
              {:else}
                <Copy class="h-4 w-4 md:h-3.5 md:w-3.5" />
              {/if}
            </button>
          {/if}
        </div>
      </div>
    </article>
  </div>
{:else}
  <article class={`overflow-hidden rounded-[1.4rem] px-3 py-2.5 shadow-card sm:rounded-3xl sm:px-4 sm:py-3 ${sizeClass()} ${bubbleClass()}`}>
    {#if item.html}
      <div use:addCodeCopyButtons={item.html} class={`chat-markdown prose max-w-none overflow-x-auto break-words prose-pre:overflow-x-auto ${proseClass()}`}>{@html item.html}</div>
    {:else}
      <p class="whitespace-pre-wrap break-words text-sm leading-6 [overflow-wrap:anywhere]">{item.content}</p>
    {/if}

    {#if item.attachments && item.attachments.length > 0}
      <div class="mt-4 space-y-3">
        {#each item.attachments as attachment}
          {#if attachment.url && isImage(attachment.mime_type)}
            <a href={attachment.url} target="_blank" rel="noreferrer" class="block overflow-hidden rounded-2xl border border-slate-800 bg-slate-950/60">
              <img src={attachment.url} alt={attachment.filename} class="max-h-80 w-full object-cover" loading="lazy" />
              <div class="px-3 py-2 text-xs text-slate-400">{attachment.filename}</div>
            </a>
          {:else if attachment.url}
            <a href={attachment.url} target="_blank" rel="noreferrer" class="flex items-center justify-between rounded-2xl border border-slate-800 bg-slate-950/60 px-3 py-3 text-sm text-slate-200 hover:border-slate-600">
              <span class="truncate">{attachment.filename}</span>
              <span class="text-xs text-slate-500">{attachment.mime_type}</span>
            </a>
          {:else}
            <div class="rounded-2xl border border-slate-800 bg-slate-950/60 px-3 py-3 text-sm text-slate-300">{attachment.filename}</div>
          {/if}
        {/each}
      </div>
    {/if}

    <div class="mt-2 flex items-center justify-end gap-2 text-[11px] opacity-70 sm:mt-2.5">
      <span title={formatAbsoluteTime(item.timestamp)}>{formatCompactTime(item.timestamp, nowDate)}</span>
      {#if item.streaming}
        <LiveDots inline={true} size="sm" tone="slate" />
        <span class="sr-only">Streaming</span>
      {/if}
    </div>
  </article>
{/if}
