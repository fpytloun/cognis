<script lang="ts">
  import Check from 'lucide-svelte/icons/check';
  import Copy from 'lucide-svelte/icons/copy';
  import Square from 'lucide-svelte/icons/square';
  import Volume2 from 'lucide-svelte/icons/volume-2';
  import { onMount } from 'svelte';
  import { api } from '$lib/api/client';
  import type { MessageTimelineItem } from '$lib/chat';
  import AgentAvatar from '$lib/components/AgentAvatar.svelte';
  import AgentProfilePopover from '$lib/components/AgentProfilePopover.svelte';
  import LiveDots from '$lib/components/LiveDots.svelte';
  import MessageAttachments from '$lib/components/MessageAttachments.svelte';
  import { audioPlayer } from '$lib/stores/audio-player';
  import { addToast } from '$lib/stores/toasts';
  import { now as nowStore } from '$lib/stores/now';
  import { formatAbsoluteTime, formatCompactTime } from '$lib/time';
  import type { Agent } from '$lib/types/api';

  let {
    item,
    agent = null,
    compact = false,
    searchQuery = '',
    searchActive = false,
    searchSelected = false
  } = $props<{
    item: MessageTimelineItem;
    agent?: Agent | null;
    /**
     * Diagnostic / log-viewer mode. Drops the leading avatar column
     * and the max-width cap so the message text takes the full
     * container width — used inside SessionLogsDrawer where the
     * reader wants to read raw conversation content rather than a
     * styled chat view.
     */
    compact?: boolean;
    searchQuery?: string;
    searchActive?: boolean;
    searchSelected?: boolean;
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
    if (compact) return 'w-full min-w-0';
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
    // Outgoing bubble uses a tonal sky-600 with a subtle border and light
    // text instead of the previous vivid sky-500 + near-black text. The
    // result is clearly an outgoing message but reads quietly against the
    // slate-950 conversation surface, matching the rest of the chat style.
    return item.role === 'user'
      ? 'ml-auto bg-sky-600/90 text-slate-50 border border-sky-400/30'
      : item.role === 'assistant'
        ? 'bg-slate-900 text-slate-100 border border-slate-800'
        : 'bg-slate-800/70 text-slate-200 border border-slate-700';
  }

  const agentDefaultChatMode = $derived(
    agent?.execution?.default_chat_mode === 'plan' || agent?.execution?.default_chat_mode === 'build'
      ? agent.execution.default_chat_mode
      : 'default'
  );
  const explicitChatMode = $derived(
    item.chatMode && item.chatMode !== 'default' && item.chatMode !== agentDefaultChatMode ? item.chatMode : undefined
  );

  function modeBadgeClass(): string {
    return explicitChatMode === 'build'
      ? 'border-amber-300/45 bg-amber-300/10 text-amber-100'
      : 'border-sky-300/30 bg-sky-300/[0.075] text-sky-100';
  }

  function modeClass(): string {
    if (explicitChatMode === 'plan') return 'border-l-4 border-l-sky-300/60 bg-sky-300/[0.025]';
    if (explicitChatMode === 'build') return 'border-l-4 border-l-amber-300/85 bg-amber-300/[0.055]';
    return '';
  }

  function proseClass(): string {
    return item.role === 'user'
      ? 'prose-user'
      : 'prose-invert prose-code:text-sky-200 prose-code:before:content-none prose-code:after:content-none';
  }

  function searchRingClass(): string {
    if (searchSelected) return 'ring-2 ring-yellow-300/80 ring-offset-2 ring-offset-slate-950';
    if (searchActive) return 'ring-1 ring-yellow-300/35 ring-offset-1 ring-offset-slate-950';
    return '';
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

  // ---- TTS speaker button -------------------------------------------------

  const ttsKey = $derived(item.messageId ?? `local:${item.timestamp}`);
  const ttsCacheMessageId = $derived(item.messageId ? `${item.messageId}:tts_full` : null);
  const audioState = $derived($audioPlayer);
  // True while the audio is actively playing — show Stop icon.
  const isSpeakingThis = $derived(
    audioState.currentKey === ttsKey && audioState.isPlaying
  );

  let ttsBusy = $state(false);

  // True while the synthesize HTTP call is in flight OR the audio element
  // is preparing the stream — show a spinner.
  const isLoadingThis = $derived(
    ttsBusy || (audioState.currentKey === ttsKey && audioState.isLoading)
  );

  async function toggleSpeak(): Promise<void> {
    if (audioPlayer.isCurrent(ttsKey)) {
      audioPlayer.stop();
      return;
    }
    if (ttsBusy) return;
    if (!item.content || !item.content.trim()) return;
    ttsBusy = true;
    try {
      const result = await api.tts.synthesize({
        text: item.content,
        message_id: ttsCacheMessageId,
        agent_id: agent?.agent_id ?? null
      });
      await audioPlayer.play(ttsKey, result.audio_url);
    } catch (err) {
      const message = err instanceof Error ? err.message : 'Failed to read message aloud';
      addToast(message, 'error', 4_000, 'Text-to-speech failed');
    } finally {
      ttsBusy = false;
    }
  }

  function codeCopyIcon(copied: boolean): string {
    if (copied) {
      return '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M20 6 9 17l-5-5"></path></svg><span class="sr-only">Copied code block</span>';
    }
    return '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="9" y="9" width="13" height="13" rx="2" ry="2"></rect><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"></path></svg><span class="sr-only">Copy code block</span>';
  }

  function escapeRegExp(value: string): string {
    return value.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  }

  function clearSearchHighlights(node: HTMLElement): void {
    for (const mark of Array.from(node.querySelectorAll<HTMLElement>('mark[data-chat-search-highlight="1"]'))) {
      mark.replaceWith(document.createTextNode(mark.textContent ?? ''));
    }
    node.normalize();
  }

  function shouldSkipHighlightNode(node: Node): boolean {
    const parent = node.parentElement;
    return !parent || Boolean(parent.closest('pre, code, button, a, input, textarea, mark'));
  }

  function applySearchHighlights(node: HTMLElement, query: string): void {
    clearSearchHighlights(node);
    const q = query.trim();
    if (!q) return;

    const regex = new RegExp(escapeRegExp(q), 'gi');
    const walker = document.createTreeWalker(node, NodeFilter.SHOW_TEXT, {
      acceptNode(textNode) {
        if (shouldSkipHighlightNode(textNode)) return NodeFilter.FILTER_REJECT;
        const text = textNode.textContent ?? '';
        regex.lastIndex = 0;
        return regex.test(text) ? NodeFilter.FILTER_ACCEPT : NodeFilter.FILTER_REJECT;
      }
    });
    const textNodes: Text[] = [];
    while (walker.nextNode()) textNodes.push(walker.currentNode as Text);

    for (const textNode of textNodes) {
      const text = textNode.textContent ?? '';
      regex.lastIndex = 0;
      let lastIndex = 0;
      let match: RegExpExecArray | null;
      const fragment = document.createDocumentFragment();
      while ((match = regex.exec(text)) !== null) {
        if (match.index > lastIndex) {
          fragment.append(document.createTextNode(text.slice(lastIndex, match.index)));
        }
        const mark = document.createElement('mark');
        mark.dataset.chatSearchHighlight = '1';
        mark.className = 'chat-search-mark';
        mark.textContent = match[0];
        fragment.append(mark);
        lastIndex = match.index + match[0].length;
      }
      if (lastIndex < text.length) fragment.append(document.createTextNode(text.slice(lastIndex)));
      textNode.replaceWith(fragment);
    }
  }

  function highlightSearch(
    node: HTMLElement,
    params: { query: string; active: boolean }
  ): { update: (next: { query: string; active: boolean }) => void; destroy: () => void } {
    const sync = (next: { query: string; active: boolean }): void => {
      if (next.active) applySearchHighlights(node, next.query);
      else clearSearchHighlights(node);
    };
    sync(params);
    return {
      update(next) {
        sync(next);
      },
      destroy() {
        clearSearchHighlights(node);
      }
    };
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
    // Track which button a copyKey currently maps to, so we can clear
    // timers for keys whose pre has been replaced.
    const buttonByKey = new Map<string, HTMLButtonElement>();

    const cancelTimer = (copyKey: string): void => {
      const existing = codeCopyResetTimers.get(copyKey);
      if (existing !== undefined) {
        window.clearTimeout(existing);
        codeCopyResetTimers.delete(copyKey);
      }
    };

    const markCopied = (copyKey: string, button: HTMLButtonElement): void => {
      copiedCodeBlocks = new Set([...copiedCodeBlocks, copyKey]);
      button.innerHTML = codeCopyIcon(true);
      cancelTimer(copyKey);
      const reset = window.setTimeout(() => {
        const next = new Set(copiedCodeBlocks);
        next.delete(copyKey);
        copiedCodeBlocks = next;
        codeCopyResetTimers.delete(copyKey);
        // Only touch the button if it's still in the DOM — if the streaming
        // rerender replaced the <pre>, we'd be writing to a detached node.
        const current = buttonByKey.get(copyKey);
        if (current && current.isConnected) current.innerHTML = codeCopyIcon(false);
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

        let wrapper = block.parentElement;
        if (!wrapper?.classList.contains('chat-code-wrap')) {
          wrapper = document.createElement('div');
          wrapper.className = 'chat-code-wrap';
          block.before(wrapper);
          wrapper.append(block);
        }

        const button = document.createElement('button');
        button.type = 'button';
        button.className = 'copy-icon-button chat-code-copy-button';
        button.setAttribute('aria-label', 'Copy code block');
        button.title = 'Copy code block';
        button.innerHTML = codeCopyIcon(copiedCodeBlocks.has(copyKey));

        const onClick = async (): Promise<void> => {
          try {
            await navigator.clipboard.writeText(code.textContent ?? '');
            markCopied(copyKey, button);
          } catch {
            addToast('Failed to copy code block', 'error');
          }
        };

        button.addEventListener('click', onClick);
        wrapper.append(button);
        buttonByKey.set(copyKey, button);
      }

      // Cancel timers for copyKeys whose <pre> has been removed by a
      // streaming tail rebuild, so we don't attempt to restore text on a
      // detached label.
      for (const copyKey of buttonByKey.keys()) {
        if (!seenKeys.has(copyKey)) {
          cancelTimer(copyKey);
          buttonByKey.delete(copyKey);
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
        for (const copyKey of buttonByKey.keys()) cancelTimer(copyKey);
        buttonByKey.clear();
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
  <div class={`flex w-full min-w-0 items-start ${compact ? '' : 'gap-2 sm:gap-3'}`}>
    {#if !compact}
      <div class="relative mt-0.5 shrink-0">
        {#if agent}
          <button
            type="button"
            class="block rounded-2xl transition hover:ring-2 hover:ring-sky-300/40 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sky-300"
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
    {/if}
    <article class={`overflow-hidden rounded-[1.4rem] px-3 py-2.5 shadow-card transition sm:rounded-3xl sm:px-4 sm:py-3 ${sizeClass()} ${bubbleClass()} ${modeClass()} ${searchRingClass()}`}>
      {#if item.html}
        <div use:addCodeCopyButtons={item.html} use:highlightSearch={{ query: searchQuery, active: searchActive }} class={`chat-markdown prose max-w-none overflow-x-auto break-words prose-pre:overflow-x-auto ${proseClass()}`}>{@html item.html}</div>
      {:else}
        <p use:highlightSearch={{ query: searchQuery, active: searchActive }} class="whitespace-pre-wrap break-words text-sm leading-6 [overflow-wrap:anywhere]">{item.content}</p>
      {/if}

      {#if item.attachments && item.attachments.length > 0}
        <MessageAttachments attachments={item.attachments} />
      {/if}

      <div class="mt-2.5 flex items-center justify-between gap-3 text-[11px] opacity-80 sm:mt-3">
        <div class="flex min-w-0 items-center gap-1.5">
          <AgentAvatar name={agentName} avatarUrl={agentAvatarUrl} class="h-4 w-4 rounded-md text-[9px]" />
          <span class="truncate font-medium text-slate-200">{agentName}</span>
          {#if explicitChatMode === 'plan' || explicitChatMode === 'build'}
            <span class={`rounded-full border px-1.5 py-0.5 text-[9px] font-semibold uppercase tracking-[0.16em] ${modeBadgeClass()}`} title={item.chatModeSource ?? `${explicitChatMode} mode`}>{explicitChatMode}</span>
          {/if}
          {#if item.partial}
            <span class="rounded-full border border-slate-600 bg-slate-800/80 px-1.5 py-0.5 text-[9px] font-semibold uppercase tracking-[0.16em] text-slate-300" title={item.finishReason === 'user_cancelled' ? 'Response was stopped by the user before completion' : 'Partial response'}>partial</span>
          {/if}
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
              class="copy-icon-button"
              onclick={toggleSpeak}
              type="button"
              title={isLoadingThis ? 'Loading audio…' : isSpeakingThis ? 'Stop reading' : 'Read aloud'}
              aria-label={isLoadingThis ? 'Loading audio' : isSpeakingThis ? 'Stop reading' : 'Read aloud'}
              aria-pressed={isSpeakingThis}
              disabled={isLoadingThis}
            >
              {#if isLoadingThis}
                <span class="inline-block h-3.5 w-3.5 animate-spin rounded-full border border-current border-t-transparent" aria-hidden="true"></span>
              {:else if isSpeakingThis}
                <Square />
              {:else}
                <Volume2 />
              {/if}
            </button>
            <button
              class="copy-icon-button"
              onclick={copyMessage}
              type="button"
              title="Copy raw markdown"
              aria-label="Copy raw markdown"
            >
              {#if messageCopied}
                <Check />
              {:else}
                <Copy />
              {/if}
            </button>
          {/if}
        </div>
      </div>
    </article>
  </div>
{:else}
  <article class={`overflow-hidden rounded-[1.4rem] px-3 py-2.5 shadow-card transition sm:rounded-3xl sm:px-4 sm:py-3 ${sizeClass()} ${bubbleClass()} ${modeClass()} ${searchRingClass()}`}>
    {#if item.html}
      <div use:addCodeCopyButtons={item.html} use:highlightSearch={{ query: searchQuery, active: searchActive }} class={`chat-markdown prose max-w-none overflow-x-auto break-words prose-pre:overflow-x-auto ${proseClass()}`}>{@html item.html}</div>
    {:else}
      <p use:highlightSearch={{ query: searchQuery, active: searchActive }} class="whitespace-pre-wrap break-words text-sm leading-6 [overflow-wrap:anywhere]">{item.content}</p>
    {/if}

    {#if item.attachments && item.attachments.length > 0}
      <MessageAttachments attachments={item.attachments} />
    {/if}

    <div class="mt-2 flex items-center justify-end gap-2 text-[11px] opacity-70 sm:mt-2.5">
      {#if explicitChatMode === 'plan' || explicitChatMode === 'build'}
        <span class={`rounded-full border px-1.5 py-0.5 text-[9px] font-semibold uppercase tracking-[0.16em] ${modeBadgeClass()}`} title={item.chatModeSource ?? `${explicitChatMode} mode`}>{explicitChatMode}</span>
      {/if}
      <span title={formatAbsoluteTime(item.timestamp)}>{formatCompactTime(item.timestamp, nowDate)}</span>
      {#if item.streaming}
        <LiveDots inline={true} size="sm" tone="slate" />
        <span class="sr-only">Streaming</span>
      {/if}
    </div>
  </article>
{/if}

<style>
  :global(.chat-search-mark) {
    border-radius: 0.25rem;
    background: rgba(250, 204, 21, 0.45);
    color: inherit;
    padding: 0 0.08em;
  }
</style>
