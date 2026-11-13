<script lang="ts">
  import Check from 'lucide-svelte/icons/check';
  import Copy from 'lucide-svelte/icons/copy';
  import GitFork from 'lucide-svelte/icons/git-fork';
  import LoaderCircle from 'lucide-svelte/icons/loader-circle';
  import Play from 'lucide-svelte/icons/play';
  import Pause from 'lucide-svelte/icons/pause';
  import RotateCcw from 'lucide-svelte/icons/rotate-ccw';
  import RotateCw from 'lucide-svelte/icons/rotate-cw';
  import Volume2 from 'lucide-svelte/icons/volume-2';
  import { getContext, onMount, onDestroy } from 'svelte';
  import { getFeedbackContext, getFeedbackSurface } from '$lib/feedback/context';
  import { getChatMessageFeedback } from '$lib/feedback/chat-feedback-context';
  import { lazyChatFeedback as chatTextFeedback } from '$lib/feedback/lazy-chat-feedback';
  import { streamReveal } from '$lib/chat-v2/stream-reveal';
  import { mermaidDiagrams } from '$lib/actions/mermaid-diagrams';
  import { CODE_COPY_FEEDBACK_MS, CODE_COPY_ICON, CODE_COPIED_ICON, CODE_EXPAND_ICON } from '$lib/chat-code-controls';
  import { chatArrivalContext, messageArrival, type ChatArrivalRegistry } from '$lib/chat-v2/motion';
  import { api } from '$lib/api/client';
  import type { MessageTimelineItem } from '$lib/timeline-render-model';
  import AgentAvatar from '$lib/components/AgentAvatar.svelte';
  import DeferredView from '$lib/components/DeferredView.svelte';
  import FeedbackIndicator from '$lib/components/feedback/FeedbackIndicator.svelte';
  import { isPrivateInstruction, remoteMessageSender } from '$lib/managed-channel';
  import LiveDots from '$lib/components/LiveDots.svelte';
  import MessageAttachments from '$lib/components/MessageAttachments.svelte';
  import Popover from '$lib/components/ui/Popover.svelte';
  import { audioPlayer } from '$lib/stores/audio-player';
  import PlaybackSpeedButton from '$lib/components/PlaybackSpeedButton.svelte';
  import { autoplayDenied } from '$lib/utils/audio-priming';
  import { SpeechPlayback } from '$lib/utils/speech-playback';
  import { addToast } from '$lib/stores/toasts';
  import { now as nowStore } from '$lib/stores/now';
  import { formatAbsoluteTime, formatCompactTime } from '$lib/time';
  import type { Agent } from '$lib/types/api';
  import { cancellationOriginLabel } from '$lib/cancellation-reason';
  import {
    forkAssistantMessageContext,
    type ForkAssistantMessage
  } from '$lib/chat-message-actions';

  let {
    item,
    agent = null,
    compact = false,
    searchQuery = '',
    searchActive = false,
    searchSelected = false,
    live = false
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
    live?: boolean;
  }>();
  const arrivals = getContext<ChatArrivalRegistry | undefined>(chatArrivalContext);
  const feedbackSurface = getFeedbackSurface(), feedbackOrigin = getFeedbackContext(), chatFeedback = getChatMessageFeedback();
  let feedbackError = $state('');
  const feedbackOptions = $derived({
    surface: feedbackSurface, binding: feedbackSurface?.binding,
    key: JSON.stringify([chatFeedback?.sourceKey, feedbackOrigin.context, item.id, item.content]),
    enabled: Boolean(chatFeedback?.eligible && item.role === 'assistant' && !item.streaming),
    text: item.content,
    error: (message: string) => { feedbackError = message; },
  });

  const runtimeAgentName = $derived(
    item.runtime?.agent_display_name
      ?? item.runtime?.agent_name
      ?? item.runtime?.agent_id
      ?? null
  );
  const agentName = $derived(
    agent ? (agent.display_name ?? agent.name) : runtimeAgentName ?? 'Assistant'
  );
  const agentAvatarUrl = $derived(agent?.avatar_url ?? null);
  const forkAssistantMessage = getContext<ForkAssistantMessage | undefined>(
    forkAssistantMessageContext
  );
  const partialStatusLabel = $derived(
    item.finishReason === 'user_cancelled'
      ? cancellationOriginLabel('user')
      : 'partial'
  );
  const partialStatusTitle = $derived(
    item.finishReason === 'user_cancelled'
      ? 'Response was stopped by the user before completion'
      : 'Partial response'
  );

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
  let forking = $state(false);
  const canFork = $derived(
    !compact
    && !item.streaming
    && Boolean(item.sessionId)
    && item.seq !== null
    && forkAssistantMessage !== undefined
  );
  const codeCopyResetTimers = new Map<string, number>();
  let expandedCode = $state<{ text: string; language: string | null } | null>(null);

  function sizeClass(): string {
    if (compact) return 'w-full min-w-0';
    return item.role === 'assistant'
      ? 'min-w-0 flex-1'
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
    if (remoteSender) return 'chat-message-remote mr-auto border border-blue-border bg-blue-subtle text-foreground';
    return item.role === 'user'
      ? 'chat-message-user ml-auto border'
      : item.role === 'assistant'
        ? 'chat-message-assistant border'
        : 'bg-inset/70 text-foreground border border-control-border';
  }

  const agentDefaultChatMode = $derived(
    agent?.execution?.default_chat_mode === 'plan' || agent?.execution?.default_chat_mode === 'build'
      ? agent.execution.default_chat_mode
      : 'default'
  );
  const explicitChatMode = $derived(
    item.chatMode && item.chatMode !== 'default' && item.chatMode !== agentDefaultChatMode ? item.chatMode : undefined
  );
  const runtimeProfileLabel = $derived.by(() => {
    const runtime = item.runtime;
    if (!runtime?.agent_profile_id) return null;
    if (runtime.agent_profile_synthetic || runtime.agent_profile_id === 'default') return null;
    return runtime.agent_profile_id;
  });
  const runtimeTooltip = $derived.by(() => {
    const runtime = item.runtime;
    const lines = [`Agent: ${agentName}`];
    if (runtimeProfileLabel) lines.push(`Agent profile: ${runtimeProfileLabel}`);
    if (runtime?.model) lines.push(`Model: ${runtime.model}`);
    if (runtime?.reasoning_effort) lines.push(`Thinking effort: ${runtime.reasoning_effort}`);
    if (runtime?.reasoning_mode) lines.push(`Thinking mode: ${runtime.reasoning_mode}`);
    return lines.join('\n');
  });
  const deliveryStatusLabel = $derived(
    item.role === 'user' && item.deliveryStatus
      ? item.deliveryStatus
      : undefined
  );

  function modeBadgeClass(): string {
    return explicitChatMode === 'build'
      ? 'border-orange-border bg-orange-subtle text-orange-tone'
      : 'border-blue-border bg-blue-subtle text-blue-tone';
  }

  function modeClass(): string {
    if (explicitChatMode === 'plan') return 'border-l-4 border-l-blue-border';
    if (explicitChatMode === 'build') return 'border-l-4 border-l-orange-border';
    return '';
  }

  function deliveryStatusClass(): string {
    if (deliveryStatusLabel === 'queued') return 'border-info-border bg-info-subtle text-info';
    if (deliveryStatusLabel === 'failed') return 'border-danger-border bg-danger-subtle text-danger';
    return 'border-current text-inherit';
  }

  function proseClass(): string {
    return item.role === 'user' && !remoteSender
      // The bubble owns its foreground pair, independently of action colors.
      ? 'prose-user prose-code:before:content-none prose-code:after:content-none'
      : 'prose-code:text-accent prose-code:before:content-none prose-code:after:content-none';
  }

  function searchRingClass(): string {
    if (searchSelected) return 'ring-2 ring-accent/80 ring-offset-2 ring-offset-canvas';
    if (searchActive) return 'ring-1 ring-warning/35 ring-offset-1 ring-offset-canvas';
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

  async function forkMessage(): Promise<void> {
    if (!canFork || forking || !item.sessionId || item.seq === null || !forkAssistantMessage) return;
    forking = true;
    try {
      await forkAssistantMessage(item.sessionId, item.seq);
    } finally {
      forking = false;
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
  let ttsController: AbortController | null = null;
  onDestroy(() => {
    ttsController?.abort();
    audioPlayer.stopIfKey(ttsKey);
  });

  // True while the synthesize HTTP call is in flight OR the audio element
  // is preparing the stream — show a spinner.
  const isLoadingThis = $derived(
    ttsBusy || (audioState.currentKey === ttsKey && audioState.isLoading)
  );

  async function toggleSpeak(): Promise<void> {
    if (audioPlayer.isCurrent(ttsKey)) {
      if (isLoadingThis) audioPlayer.stop();
      else {
        try { await audioPlayer.togglePause(); }
        catch { addToast('Playback could not resume. Try reading the message again.', 'error'); audioPlayer.stop(); }
      }
      return;
    }
    if (ttsBusy) return;
    if (!item.content || !item.content.trim()) return;
    ttsBusy = true;
    const controller = new AbortController();
    ttsController = controller;
    audioPlayer.prepare(ttsKey, () => controller.abort());
    try {
      // Must start during the click handler. The synthesis request below
      // crosses an async boundary and would otherwise lose user activation
      // on browsers with strict autoplay policies.
      await audioPlayer.unlock(controller.signal);
      if (controller.signal.aborted) return;
      if (SpeechPlayback.supported()) {
        const capabilities = await api.tts.capabilities(agent?.agent_id);
        if (controller.signal.aborted) return;
        if (capabilities.streaming_tts) {
          ttsBusy = false;
          const result = await audioPlayer.playSpeech(ttsKey, (signal) => api.tts.stream({
            text: item.content, message_id: ttsCacheMessageId,
            agent_id: agent?.agent_id ?? null, use_user_preferences: true,
          }, { signal, timeoutMs: 600_000 }), controller.signal);
          for (const warning of result.warnings ?? []) addToast(warning, 'warning', 8_000, 'Speech voice');
          return;
        }
      }
      const result = await api.tts.synthesize({
        text: item.content,
        message_id: ttsCacheMessageId,
        agent_id: agent?.agent_id ?? null,
        use_user_preferences: true
      }, { signal: controller.signal, timeoutMs: 600_000 });
      if (controller.signal.aborted || !audioPlayer.isCurrent(ttsKey)) return;
      for (const warning of result.warnings ?? []) addToast(warning, 'warning', 8_000, 'Speech voice');
      await audioPlayer.play(ttsKey, result.parts?.length ? result.parts.map((part) => part.audio_url) : result.audio_url);
    } catch (err) {
      if (controller.signal.aborted) return;
      if (autoplayDenied(err) && audioPlayer.isCurrent(ttsKey)) {
        addToast('The recording is ready. Tap the speaker again to start playback.', 'info', 8_000, 'Playback needs a tap');
        return;
      }
      audioPlayer.stopIfKey(ttsKey);
      const message = err instanceof Error ? err.message : 'Failed to read message aloud';
      addToast(message, 'error', 4_000, 'Text-to-speech failed');
    } finally {
      ttsBusy = false;
      if (ttsController === controller) ttsController = null;
    }
  }

  function codeCopyIcon(copied: boolean): string {
    return `${copied ? CODE_COPIED_ICON : CODE_COPY_ICON}<span class="sr-only">${copied ? 'Copied code block' : 'Copy code block'}</span>`;
  }

  function codeLanguage(code: HTMLElement): string | null {
    for (const className of code.classList) {
      if (className.startsWith('language-')) return className.slice('language-'.length);
    }
    return null;
  }

  function openCodeModal(code: HTMLElement): void {
    expandedCode = {
      text: code.textContent ?? '',
      language: codeLanguage(code)
    };
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
    return !parent || Boolean(parent.closest('pre, code, button, a, input, textarea, mark, [data-mermaid-viewer]'));
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
    params: { query: string; active: boolean; selected: boolean }
  ): { update: (next: { query: string; active: boolean; selected: boolean }) => void; destroy: () => void } {
    const sync = (next: { query: string; active: boolean; selected: boolean }): void => {
      node.classList.toggle('chat-search-current', next.selected);
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
      }, CODE_COPY_FEEDBACK_MS);
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

        const actions = document.createElement('div');
        actions.className = 'chat-code-actions';

        const expandButton = document.createElement('button');
        expandButton.type = 'button';
        expandButton.className = 'copy-icon-button';
        expandButton.setAttribute('aria-label', 'Open code block in modal');
        expandButton.title = 'Open code block in modal';
        expandButton.innerHTML = `${CODE_EXPAND_ICON}<span class="sr-only">Open code block in modal</span>`;
        expandButton.addEventListener('click', () => openCodeModal(code));

        const button = document.createElement('button');
        button.type = 'button';
        button.className = 'copy-icon-button';
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
        actions.append(expandButton, button);
        wrapper.append(actions);
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
  const remoteSender = $derived(item.role === 'user' ? remoteMessageSender(item.messageMetadata) : null);
  const privateInstruction = $derived(item.role === 'user' && isPrivateInstruction(item.messageMetadata));
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
  <div use:messageArrival={{ id: item.id, eligible: !compact && item.arrivalEligible === true, registry: arrivals }}
    class={`flex w-full min-w-0 items-start ${compact ? '' : 'gap-2 sm:gap-3'}`}>
    {#if !compact}
      <div class="chat-leading-avatar relative mt-0.5 shrink-0" data-copy-exclude>
        {#if agent}
          <button
            type="button"
            class="block rounded-2xl transition hover:ring-2 hover:ring-accent/40 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent"
            aria-label={`View ${agentName} profile`}
            onclick={() => { showAgentProfile = !showAgentProfile; }}
          >
            <AgentAvatar name={agentName} avatarUrl={agentAvatarUrl} class="h-8 w-8 text-xs" />
          </button>
          {#if showAgentProfile}
            {@const profileAgent = agent}
            <DeferredView load={() => import('$lib/components/AgentProfilePopover.svelte')}
              label="Loading agent profile…" onCancel={() => { showAgentProfile = false; }}>
              {#snippet children(loaded)}
                <loaded.default agent={profileAgent} onClose={() => { showAgentProfile = false; }} />
              {/snippet}
            </DeferredView>
          {/if}
        {:else}
          <AgentAvatar name={agentName} avatarUrl={agentAvatarUrl} class="h-8 w-8 text-xs" />
        {/if}
      </div>
    {/if}
    <article data-message-role={item.role} class={`chat-message-bubble overflow-hidden rounded-[1.4rem] px-3 py-2.5 shadow-card transition sm:rounded-3xl sm:px-4 sm:py-3 ${sizeClass()} ${bubbleClass()} ${modeClass()} ${searchRingClass()}`}>
      {#if item.html !== undefined && item.html !== null}
        <div class:streaming-markdown={item.streaming} use:chatTextFeedback={feedbackOptions} use:addCodeCopyButtons={item.html} use:mermaidDiagrams={{ html: item.html, streaming: item.streaming }} use:highlightSearch={{ query: searchQuery, active: searchActive, selected: searchSelected }} use:streamReveal={{ html: item.html, streaming: Boolean(item.streaming) }} class={`chat-markdown prose max-w-none overflow-x-auto break-words prose-pre:overflow-x-auto ${proseClass()}`}>{@html item.html}</div>
      {:else}
        <p use:chatTextFeedback={feedbackOptions} use:highlightSearch={{ query: searchQuery, active: searchActive, selected: searchSelected }} class="chat-message-text whitespace-pre-wrap break-words leading-6 [overflow-wrap:anywhere]">{item.content}</p>
      {/if}

      {#if feedbackError}<p role="alert" class="text-xs text-danger" data-copy-exclude>{feedbackError}</p>{/if}
      {#if item.commentReplies?.length}
        <DeferredView load={() => import('$lib/components/feedback/AgentCommentReplies.svelte')} label="">
          {#snippet children(loaded)}<loaded.default replies={item.commentReplies} />{/snippet}
        </DeferredView>
      {/if}
      {#if item.attachments && item.attachments.length > 0}
        <MessageAttachments attachments={item.attachments} />
      {/if}

      <div class="chat-message-meta mt-2.5 flex flex-wrap items-center justify-between gap-3 text-[11px] opacity-80 sm:mt-3" data-copy-exclude>
        <div class="flex min-w-0 items-center gap-1.5">
          <AgentAvatar name={agentName} avatarUrl={agentAvatarUrl} class="h-4 w-4 rounded-md text-[9px]" />
          <Popover text={runtimeTooltip} placement="top">
            <span class="truncate font-medium text-foreground">{agentName}</span>
          </Popover>
          {#if explicitChatMode === 'plan' || explicitChatMode === 'build'}
            <span class={`rounded-full border px-1.5 py-0.5 text-[9px] font-semibold uppercase tracking-[0.16em] ${modeBadgeClass()}`} title={item.chatModeSource ?? `${explicitChatMode} mode`}>{explicitChatMode}</span>
          {/if}
          {#if item.partial && !item.streaming}
            <span class="rounded-full border border-control-border bg-raised/80 px-1.5 py-0.5 text-[9px] font-semibold uppercase tracking-[0.16em] text-secondary" title={partialStatusTitle}>{partialStatusLabel}</span>
          {/if}
          <span class="legacy-quiet text-quiet">·</span>
          <span class="whitespace-nowrap text-muted" title={formatAbsoluteTime(item.timestamp)}>{formatCompactTime(item.timestamp, nowDate)}</span>
        </div>
        <div class="ml-auto flex shrink-0 items-center gap-2">
          {#if item.streaming}
            <!-- The animated dots alone convey streaming; the "Live" label was
                 visual noise. An sr-only span keeps an accessible name. -->
            <LiveDots inline={true} size="sm" tone="sky" />
            <span class="sr-only">Streaming</span>
          {/if}
          {#if !item.streaming}
            <div class="message-playback" role="group" aria-label="Speech playback">
            {#if audioState.currentKey === ttsKey && !isLoadingThis}
              <button type="button" aria-label="Back 10 seconds" title="Back 10 seconds"
                onclick={() => audioPlayer.seek(-10)} class="copy-icon-button message-seek-button relative">
                <RotateCcw /><span aria-hidden="true" class="absolute text-[7px]">10</span>
              </button>
            {/if}
            <button
              class="copy-icon-button"
              onclick={toggleSpeak}
              type="button"
              title={isLoadingThis ? 'Cancel audio generation' : isSpeakingThis ? 'Pause reading' : $audioPlayer.currentKey === ttsKey ? 'Resume reading' : 'Read aloud'}
              aria-label={isLoadingThis ? 'Cancel audio generation' : isSpeakingThis ? 'Pause reading' : $audioPlayer.currentKey === ttsKey ? 'Resume reading' : 'Read aloud'}
              aria-pressed={isSpeakingThis}
            >
              {#if isLoadingThis}
                <Volume2 class="animate-pulse" />
              {:else if isSpeakingThis}
                <Pause />
              {:else if $audioPlayer.currentKey === ttsKey}
                <Play />
              {:else}
                <Volume2 />
              {/if}
            </button>
            {#if audioState.currentKey === ttsKey && !isLoadingThis}
              <button type="button" aria-label="Forward 10 seconds" title="Forward 10 seconds"
                onclick={() => audioPlayer.seek(10)} class="copy-icon-button message-seek-button relative">
                <RotateCw /><span aria-hidden="true" class="absolute text-[7px]">10</span>
              </button>
              <PlaybackSpeedButton rate={audioState.playbackRate} onchange={audioPlayer.setPlaybackRate} />
            {/if}
            </div>
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
            {#if feedbackOptions.enabled}<FeedbackIndicator variant="message" />{/if}
            {#if canFork}
              <button
                class="copy-icon-button ml-1 border-l border-control-border"
                onclick={forkMessage}
                type="button"
                title={forking ? 'Forking conversation…' : 'Fork conversation from this message'}
                aria-label={forking ? 'Forking conversation…' : 'Fork conversation from this message'}
                aria-busy={forking}
                disabled={forking}
              >
                {#if forking}
                  <LoaderCircle class="animate-spin" />
                {:else}
                  <GitFork />
                {/if}
              </button>
            {/if}
          {/if}
        </div>
      </div>
    </article>
  </div>
{:else}
  <article use:messageArrival={{ id: item.id, eligible: !compact && item.arrivalEligible === true, registry: arrivals }}
    data-message-role={item.role} data-message-origin={remoteSender ? 'participant' : privateInstruction ? 'private-instruction' : undefined} class={`chat-message-bubble overflow-hidden rounded-[1.4rem] px-3 py-2.5 shadow-card transition sm:rounded-3xl sm:px-4 sm:py-3 ${sizeClass()} ${bubbleClass()} ${modeClass()} ${searchRingClass()}`}>
    {#if remoteSender}
      <div class="mb-2 flex items-center gap-2 text-xs font-medium" data-copy-exclude>
        <AgentAvatar name={remoteSender} class="h-6 w-6 text-[10px]" />
        <span>{remoteSender}</span>
        <span class="text-secondary">Remote participant</span>
      </div>
    {:else if privateInstruction}
      <p class="mb-2 text-xs font-medium opacity-80" data-copy-exclude>Private instruction · Not shared</p>
    {/if}
    {#if item.html}
      <div use:addCodeCopyButtons={item.html} use:mermaidDiagrams={{ html: item.html }} use:highlightSearch={{ query: searchQuery, active: searchActive, selected: searchSelected }} class={`chat-markdown prose max-w-none overflow-x-auto break-words prose-pre:overflow-x-auto ${proseClass()}`}>{@html item.html}</div>
    {:else}
      <p use:highlightSearch={{ query: searchQuery, active: searchActive, selected: searchSelected }} class="chat-message-text whitespace-pre-wrap break-words leading-6 [overflow-wrap:anywhere]">{item.content}</p>
    {/if}

    {#if item.attachments && item.attachments.length > 0}
      <MessageAttachments attachments={item.attachments} />
    {/if}
    {#if item.messageMetadata?.feedback?.submission_id}
      {@const submissionId = item.messageMetadata.feedback.submission_id}
      <DeferredView load={() => import('$lib/components/feedback/SentCommentReferences.svelte')} label="">
        {#snippet children(loaded)}<loaded.default {submissionId} />{/snippet}
      </DeferredView>
    {/if}

    <div class="mt-2 flex items-center justify-end gap-2 text-[11px] opacity-70 sm:mt-2.5" data-copy-exclude>
      {#if deliveryStatusLabel}
        <span class={`chat-state-handoff rounded-full border px-1.5 py-0.5 text-[9px] font-semibold uppercase tracking-[0.16em] ${deliveryStatusClass()}`}>{deliveryStatusLabel}</span>
      {/if}
      {#if explicitChatMode === 'plan' || explicitChatMode === 'build'}
        <span class={`rounded-full border px-1.5 py-0.5 text-[9px] font-semibold uppercase tracking-[0.16em] ${modeBadgeClass()}`} title={item.chatModeSource ?? `${explicitChatMode} mode`}>{explicitChatMode}</span>
      {/if}
      <span title={formatAbsoluteTime(item.timestamp)}>{formatCompactTime(item.timestamp, nowDate)}</span>
      {#if item.streaming && live}
        <LiveDots inline={true} size="sm" tone="slate" />
        <span class="sr-only">Streaming</span>
      {/if}
    </div>
  </article>
{/if}

{#if expandedCode}
  {@const code = expandedCode}
  <DeferredView load={() => import('$lib/components/CodeBlockModal.svelte')}
    label="Loading code viewer…" overlay onCancel={() => { expandedCode = null; }}>
  {#snippet children(loaded)}
  <loaded.default
    code={code.text}
    language={code.language}
    onClose={() => { expandedCode = null; }}
  />
  {/snippet}
  </DeferredView>
{/if}

<style>
  .message-playback { display: inline-flex; align-items: center; gap: 0; }
  .message-seek-button { border: 0; background: transparent; }
  .chat-message-assistant {
    border-radius: var(--chat-assistant-radius, 1.4rem);
  }
  @media (min-width: 640px) {
    .chat-message-assistant { border-radius: var(--chat-assistant-radius, 1.5rem); }
  }
  .chat-message-text { font-size: var(--chat-message-font-size); }
  /* One reading measure for every theme (matches rich deliverables). The
     font size here makes 70ch resolve at the message size; bubble themes cap
     the bubble, bubble-less themes let tables and code use the full column. */
  .chat-message-assistant {
    font-size: var(--chat-message-font-size);
    --chat-assistant-measure: 70ch;
    max-width: var(--chat-assistant-bubble-max-width, calc(var(--chat-assistant-measure) + 2rem));
  }
  .chat-message-assistant > .chat-message-text,
  .chat-message-assistant > .chat-markdown {
    font-family: var(--chat-prose-font);
  }
  .chat-message-assistant > .chat-message-text,
  .chat-message-assistant > .chat-message-meta,
  .chat-message-assistant > .chat-markdown > :global(:not(.markdown-table-wrap, .chat-code-wrap, pre)) {
    max-width: var(--chat-assistant-measure);
  }
  /* Paper has no bubble or leading avatar; assistant text keeps the tool rows' inset (px-3). */
  :global([data-theme-family='paper']) .chat-leading-avatar { display: none; }
  :global([data-theme-family='paper']) .chat-message-assistant {
    padding-inline: 0.75rem;
    border-inline-width: 0;
  }
  .streaming-markdown :global(.stream-reveal) {
    animation: stream-arrival var(--chat-motion-stream, 320ms) ease-out;
  }

  @keyframes stream-arrival {
    from { opacity: 0.45; }
    to { opacity: 1; }
  }

  @media (prefers-reduced-motion: reduce) {
    .streaming-markdown :global(.stream-reveal) {
      animation: none;
    }

  }

  :global(.chat-search-mark) {
    border-radius: 0.25rem;
    background: rgb(var(--color-warning) / 0.3);
    color: inherit;
    padding: 0 0.08em;
  }
  :global(.chat-search-current .chat-search-mark) {
    background: rgb(var(--color-accent) / 0.3);
  }
</style>
