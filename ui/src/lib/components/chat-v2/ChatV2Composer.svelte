<script module lang="ts">
  let slashAutocompleteInstance = 0;
</script>

<script lang="ts">
  import { onDestroy } from 'svelte';
  import ArrowUp from 'lucide-svelte/icons/arrow-up';
  import ListPlus from 'lucide-svelte/icons/list-plus';
  import Headphones from 'lucide-svelte/icons/headphones';
  import Paperclip from 'lucide-svelte/icons/paperclip';
  import Square from 'lucide-svelte/icons/square';

  import ComposerAttachments from '$lib/components/ComposerAttachments.svelte';
  import MicRecorderButton from '$lib/components/MicRecorderButton.svelte';
  import SlashCommandSuggestions from '$lib/components/chat-v2/SlashCommandSuggestions.svelte';
  import { api } from '$lib/api/client';
  import { shouldSendComposerMessage } from '$lib/chat-v2/composer-keyboard';
  import {
    applySlashSuggestion,
    localSlashCommandSuggestions,
    normalizeSlashCommandInput,
    slashParameterSuggestionCommand,
  } from '$lib/slash-commands';
  import { userPreferences } from '$lib/stores/userPreferences';
  import { hasConservativeTouchKeyboardFallback, viewportMetrics } from '$lib/stores/viewport';
  import type { AttachmentRef } from '$lib/types/api';

  let {
    value = $bindable(''),
    attachments = $bindable<AttachmentRef[]>([]),
    textarea = $bindable<HTMLTextAreaElement | null>(null),
    placeholder = 'Message',
    ariaLabel = 'Message',
    textareaTestId,
    sendAriaLabel,
    stopAriaLabel,
    disabled = false,
    busy = false,
    uploading = false,
    active = false,
    stopping = false,
    compact = false,
    showMic = true,
    allowQueue = true,
    conversationId,
    onSend,
    onStop,
    onFiles,
    onInput,
    onPaste,
    onKeydown,
    onRecorded,
    onSendRecorded,
    onOpenMode,
  } = $props<{
    value?: string;
    attachments?: AttachmentRef[];
    textarea?: HTMLTextAreaElement | null;
    placeholder?: string;
    ariaLabel?: string;
    textareaTestId?: string;
    sendAriaLabel?: string;
    stopAriaLabel?: string;
    disabled?: boolean;
    busy?: boolean;
    uploading?: boolean;
    active?: boolean;
    stopping?: boolean;
    compact?: boolean;
    showMic?: boolean;
    allowQueue?: boolean;
    conversationId?: string;
    onSend: () => void | Promise<void>;
    onStop: () => void | Promise<void>;
    onFiles: (files: File[]) => void | Promise<void>;
    onInput?: () => void;
    onPaste?: (event: ClipboardEvent) => void | Promise<void>;
    onKeydown?: (event: KeyboardEvent) => void;
    onRecorded?: (attachment: AttachmentRef) => void;
    onSendRecorded?: () => void | Promise<void>;
    onOpenMode?: () => void;
  }>();

  const hasDraft = $derived(Boolean(value.trim() || attachments.length));
  const canSend = $derived(hasDraft && !disabled && !busy && !uploading && (!active || allowQueue));
  const enterToSend = $derived($userPreferences.chat.enter_to_send);
  const softwareKeyboardOpen = $derived(
    $viewportMetrics.keyboardOpen || hasConservativeTouchKeyboardFallback()
  );
  const slashAutocompleteId = `slash-command-suggestions-${++slashAutocompleteInstance}`;
  let slashSuggestions = $state<import('$lib/types/api').SlashCommandSuggestion[]>([]);
  let slashSelectedIndex = $state(0);
  let slashSuggestionRequestSeq = 0;
  let slashSuggestionTimer: ReturnType<typeof setTimeout> | null = null;
  let slashSuggestionController: AbortController | null = null;
  let reconciledSlashValue: string | null = null;
  let reconciledConversationId: string | undefined;
  let directSendPointerId: number | null = null;
  let suppressCompatibilityClick = false;
  let compatibilityClickTimer: ReturnType<typeof setTimeout> | null = null;
  const slashSuggestionsVisible = $derived(slashSuggestions.length > 0);

  function cancelSlashSuggestionRequest(): void {
    if (slashSuggestionTimer) clearTimeout(slashSuggestionTimer);
    slashSuggestionTimer = null;
    slashSuggestionController?.abort();
    slashSuggestionController = null;
    slashSuggestionRequestSeq += 1;
  }

  function closeSlashSuggestions(): void {
    cancelSlashSuggestionRequest();
    slashSuggestions = [];
    slashSelectedIndex = 0;
  }

  function updateSlashSuggestions(): void {
    reconciledSlashValue = value;
    reconciledConversationId = conversationId;
    cancelSlashSuggestionRequest();
    const currentInput = value;
    const currentConversationId = conversationId;
    if (!currentInput.startsWith('/')) {
      slashSuggestions = [];
      slashSelectedIndex = 0;
      return;
    }
    const parameterCommand = slashParameterSuggestionCommand(currentInput);
    if (parameterCommand && currentConversationId) {
      const requestSeq = slashSuggestionRequestSeq;
      slashSuggestions = [];
      slashSelectedIndex = 0;
      const delayMs = /\s$/.test(currentInput) ? 0 : 100;
      slashSuggestionTimer = setTimeout(() => {
        slashSuggestionTimer = null;
        const controller = new AbortController();
        slashSuggestionController = controller;
        void api.conversations.slashCommandSuggestions(
          currentConversationId,
          currentInput,
          12,
          { signal: controller.signal },
        )
          .then((response) => {
            if (
              requestSeq !== slashSuggestionRequestSeq
              || controller.signal.aborted
              || value !== currentInput
              || conversationId !== currentConversationId
            ) return;
            slashSuggestionController = null;
            slashSuggestions = response.items;
            slashSelectedIndex = 0;
          })
          .catch(() => {
            if (requestSeq === slashSuggestionRequestSeq && !controller.signal.aborted) {
              slashSuggestionController = null;
              slashSuggestions = [];
              slashSelectedIndex = 0;
            }
          });
      }, delayMs);
      return;
    }
    slashSuggestions = localSlashCommandSuggestions(currentInput);
    slashSelectedIndex = 0;
  }

  $effect(() => {
    if (value === reconciledSlashValue && conversationId === reconciledConversationId) return;
    updateSlashSuggestions();
  });

  function acceptSlashSuggestion(index: number): void {
    const suggestion = slashSuggestions[index];
    if (!suggestion) return;
    value = applySlashSuggestion(suggestion);
    if (suggestion.kind === 'command' && slashParameterSuggestionCommand(value)) {
      updateSlashSuggestions();
    } else {
      reconciledSlashValue = value;
      reconciledConversationId = conversationId;
      closeSlashSuggestions();
    }
    queueMicrotask(() => textarea?.focus());
  }

  function selectSlashSuggestion(event: MouseEvent, index: number): void {
    acceptSlashSuggestion(index);
  }

  onDestroy(() => {
    cancelSlashSuggestionRequest();
    if (compatibilityClickTimer) clearTimeout(compatibilityClickTimer);
  });

  function removeAttachment(artifactId: string): void {
    attachments = attachments.filter((item: AttachmentRef) => item.artifact_id !== artifactId);
  }

  function dispatchSend(): void {
    closeSlashSuggestions();
    void onSend();
  }

  function beginDirectPointerSend(event: PointerEvent): void {
    if (event.pointerType === 'mouse' || !canSend) return;
    // Keep the textarea focused so iOS does not close the keyboard and move
    // the fixed composer between pointer-down and pointer-up.
    event.preventDefault();
    directSendPointerId = event.pointerId;
    textarea?.focus({ preventScroll: true });
    try {
      (event.currentTarget as HTMLElement).setPointerCapture(event.pointerId);
    } catch {
      // Pointer capture is optional on older WebKit.
    }
  }

  function finishDirectPointerSend(event: PointerEvent): void {
    if (directSendPointerId !== event.pointerId) return;
    event.preventDefault();
    directSendPointerId = null;
    try {
      (event.currentTarget as HTMLElement).releasePointerCapture(event.pointerId);
    } catch {
      // The send callback can synchronously replace the button.
    }
    if (!canSend) return;
    suppressCompatibilityClick = true;
    if (compatibilityClickTimer) clearTimeout(compatibilityClickTimer);
    compatibilityClickTimer = setTimeout(() => {
      suppressCompatibilityClick = false;
      compatibilityClickTimer = null;
    }, 750);
    dispatchSend();
  }

  function cancelDirectPointerSend(event: PointerEvent): void {
    if (directSendPointerId === event.pointerId) directSendPointerId = null;
  }

  function handleSendClick(event: MouseEvent): void {
    // WebKit can emit a compatibility click after the touch pointer sequence.
    // The message already dispatched on pointer-up, so suppress only that
    // immediate duplicate. Keyboard and assistive clicks have detail 0.
    const pointerType = (event as PointerEvent).pointerType;
    if (suppressCompatibilityClick && event.detail > 0 && pointerType !== 'mouse') {
      event.preventDefault();
      suppressCompatibilityClick = false;
      if (compatibilityClickTimer) clearTimeout(compatibilityClickTimer);
      compatibilityClickTimer = null;
      return;
    }
    suppressCompatibilityClick = false;
    if (compatibilityClickTimer) clearTimeout(compatibilityClickTimer);
    compatibilityClickTimer = null;
    dispatchSend();
  }

  function handleKeydown(event: KeyboardEvent): void {
    if (!event.isComposing && slashSuggestionsVisible) {
      if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
        event.preventDefault();
        const delta = event.key === 'ArrowDown' ? 1 : -1;
        slashSelectedIndex = (slashSelectedIndex + delta + slashSuggestions.length) % slashSuggestions.length;
        return;
      }
      const selectedSuggestion = slashSuggestions[slashSelectedIndex];
      const exactCommandReadyToSend = event.key === 'Enter'
        && selectedSuggestion?.kind === 'command'
        && normalizeSlashCommandInput(value) === selectedSuggestion.command
        && shouldSendComposerMessage(event, { enterToSend, softwareKeyboardOpen });
      if (
        event.key === 'Tab'
        || (
          event.key === 'Enter'
          && !exactCommandReadyToSend
          && !event.shiftKey
          && !event.ctrlKey
          && !event.metaKey
          && !event.altKey
          && !softwareKeyboardOpen
        )
      ) {
        event.preventDefault();
        acceptSlashSuggestion(slashSelectedIndex);
        return;
      }
      if (event.key === 'Escape') {
        event.preventDefault();
        closeSlashSuggestions();
        return;
      }
    }
    onKeydown?.(event);
    if (event.defaultPrevented) return;
    if (!shouldSendComposerMessage(event, { enterToSend, softwareKeyboardOpen })) return;
    event.preventDefault();
    if (canSend) dispatchSend();
  }

  function handleFiles(event: Event): void {
    const input = event.currentTarget as HTMLInputElement;
    const files = Array.from(input.files ?? []);
    input.value = '';
    if (files.length > 0) void onFiles(files);
  }
</script>

<div class:space-y-2={attachments.length > 0} data-testid="chat-v2-composer">
  {#if slashSuggestionsVisible}
    <SlashCommandSuggestions
      suggestions={slashSuggestions}
      selectedIndex={slashSelectedIndex}
      listboxId={slashAutocompleteId}
      onSelect={selectSlashSuggestion}
    />
  {/if}
  <ComposerAttachments {attachments} onremove={removeAttachment} disabled={disabled || busy || uploading} />
  <div class={`flex items-center gap-1 border border-slate-700 transition focus-within:border-sky-400/50 focus-within:ring-2 focus-within:ring-sky-300/20 ${compact ? 'rounded-2xl bg-slate-900 p-1.5' : 'rounded-3xl bg-transparent px-2 py-1'}`}>
    <label
      aria-label="Attach files"
      class="inline-flex h-9 w-9 shrink-0 cursor-pointer items-center justify-center rounded-full text-slate-400 transition hover:bg-slate-800/60 hover:text-slate-200 focus-within:bg-slate-800/60 focus-within:text-slate-200"
      class:pointer-events-none={disabled || busy || uploading}
      class:opacity-40={disabled || busy || uploading}
    >
      <Paperclip class="pointer-events-none h-4 w-4" />
      <input class="sr-only" type="file" multiple disabled={disabled || busy || uploading} onchange={handleFiles} />
    </label>
    <textarea
      bind:this={textarea}
      bind:value
      rows={1}
      class={`max-h-[200px] flex-1 resize-none self-center bg-transparent px-1 py-[0.4rem] text-[16px] leading-5 text-slate-100 placeholder:text-slate-500 focus:outline-none focus-visible:outline-none focus-visible:ring-0 focus-visible:ring-offset-0 sm:text-sm ${compact ? 'min-h-10' : 'min-h-[36px]'}`}
      {placeholder}
      aria-label={ariaLabel}
      aria-controls={slashSuggestionsVisible ? slashAutocompleteId : undefined}
      aria-activedescendant={slashSuggestionsVisible ? `${slashAutocompleteId}-option-${slashSelectedIndex}` : undefined}
      data-testid={textareaTestId}
      disabled={disabled || busy}
      enterkeyhint="enter"
      autocapitalize="sentences"
      spellcheck="true"
      onkeydown={handleKeydown}
      oninput={() => {
        onInput?.();
      }}
      onpaste={(event) => { if (onPaste) void onPaste(event); }}
    ></textarea>
    {#if canSend}
      <button
        type="button"
        aria-label={sendAriaLabel ?? (active ? 'Queue message' : 'Send')}
        title={active ? 'Queue message after current turn' : 'Send'}
        class="relative inline-flex h-9 w-9 shrink-0 touch-manipulation items-center justify-center rounded-full bg-sky-500 text-slate-950 transition duration-100 hover:bg-sky-400 active:scale-90 disabled:opacity-50"
        onpointerdown={beginDirectPointerSend}
        onpointerup={finishDirectPointerSend}
        onpointercancel={cancelDirectPointerSend}
        onclick={handleSendClick}
      >
        <ArrowUp class="h-4 w-4" stroke-width="2.5" />
        {#if active}
          <span class="absolute -bottom-0.5 -right-0.5 inline-flex h-4 w-4 items-center justify-center rounded-full border border-slate-950 bg-slate-950 text-sky-300">
            <ListPlus class="h-2.5 w-2.5" stroke-width="2.5" />
          </span>
        {/if}
      </button>
    {:else if active}
      <button
        type="button"
        aria-label={stopping ? 'Cancelling turn' : stopAriaLabel ?? 'Cancel turn'}
        title={stopping ? 'Cancelling…' : 'Cancel turn'}
        class="relative inline-flex h-9 w-9 shrink-0 items-center justify-center rounded-full bg-slate-800 text-slate-200 transition hover:bg-slate-700"
        disabled={stopping}
        onclick={() => void onStop()}
      >
        <span class:conversation-turn-orbit--stopping={stopping} class="conversation-turn-orbit" aria-hidden="true"><span></span></span>
        {#if stopping}
          <span class="conversation-turn-stop-pulse absolute h-2.5 w-2.5 animate-pulse rounded-sm bg-slate-100" aria-hidden="true"></span>
        {:else}
          <Square class="absolute h-2.5 w-2.5 fill-current text-slate-100" />
        {/if}
      </button>
    {:else if showMic}
      {#if onOpenMode}
        <button
          type="button"
          aria-label="Open conversation mode"
          title="Conversation mode"
          class="relative inline-flex h-9 w-9 shrink-0 items-center justify-center rounded-full text-slate-400 transition hover:bg-slate-800/60 hover:text-slate-200 disabled:cursor-not-allowed disabled:opacity-40"
          disabled={disabled || busy}
          onclick={onOpenMode}
        >
          <Headphones class="h-4 w-4" />
        </button>
      {/if}
      <MicRecorderButton
        disabled={disabled || busy}
        onrecorded={(attachment) => {
          attachments = [...attachments, attachment];
          onRecorded?.(attachment);
        }}
        onsendrecorded={() => onSendRecorded ? onSendRecorded() : onSend()}
      />
    {/if}
  </div>
</div>

<style>
  @keyframes conversation-turn-orbit {
    from { transform: rotate(0deg); }
    to { transform: rotate(360deg); }
  }

  .conversation-turn-orbit {
    --turn-orbit-rgb: 56 189 248;
    --turn-orbit-tip-rgb: 125 211 252;
    --turn-orbit-shadow-rgb: 14 165 233;
    position: absolute;
    inset: 0;
    border-radius: 9999px;
    pointer-events: none;
    animation: conversation-turn-orbit 1.15s linear infinite;
    background: conic-gradient(
      from 0deg,
      rgb(var(--turn-orbit-rgb) / 0) 0deg,
      rgb(var(--turn-orbit-rgb) / 0.08) 210deg,
      rgb(var(--turn-orbit-rgb) / 0.46) 315deg,
      rgb(var(--turn-orbit-tip-rgb) / 0.95) 360deg
    );
    mask: radial-gradient(farthest-side, transparent calc(100% - 3px), #000 calc(100% - 2px));
    -webkit-mask: radial-gradient(farthest-side, transparent calc(100% - 3px), #000 calc(100% - 2px));
  }

  .conversation-turn-orbit--stopping {
    opacity: 0.75;
  }

  .conversation-turn-orbit span {
    position: absolute;
    left: 50%;
    top: -1px;
    height: 0.5rem;
    width: 0.5rem;
    transform: translateX(-50%);
    border-radius: 9999px;
    background: rgb(var(--turn-orbit-rgb));
    box-shadow: 0 0 10px rgb(var(--turn-orbit-rgb) / 0.9), 0 0 18px rgb(var(--turn-orbit-shadow-rgb) / 0.45);
  }

  @media (prefers-reduced-motion: reduce) {
    .conversation-turn-orbit {
      animation: none;
      transform: rotate(45deg);
    }

    .conversation-turn-stop-pulse {
      animation: none;
    }
  }
</style>
