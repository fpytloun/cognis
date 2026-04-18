<script lang="ts">
  import { Check, Copy } from 'lucide-svelte';
  import { onMount } from 'svelte';
  import type { MessageTimelineItem } from '$lib/chat';
  import LiveDots from '$lib/components/LiveDots.svelte';
  import { addToast } from '$lib/stores/toasts';
  import { formatAbsoluteTime, formatCompactTime } from '$lib/time';

  let { item } = $props<{ item: MessageTimelineItem }>();

  let now = $state(new Date());
  let messageCopied = $state(false);
  let copiedCodeBlocks = $state<Set<string>>(new Set());
  let copyResetTimer: number | null = null;
  const codeCopyResetTimers = new Map<string, number>();

  function sizeClass(): string {
    return item.role === 'assistant'
      ? 'w-full min-w-0 xl:max-w-3xl'
      : 'w-full min-w-0 sm:max-w-[88%] xl:max-w-2xl';
  }

  onMount(() => {
    const interval = setInterval(() => { now = new Date(); }, 30_000);
    return () => {
      clearInterval(interval);
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

  function addCodeCopyButtons(node: HTMLDivElement, _html: string): { update: (_html: string) => void; destroy: () => void } {
    let cleanupCallbacks: Array<() => void> = [];

    const renderButtons = (): void => {
      cleanupCallbacks.forEach((cleanup) => cleanup());
      cleanupCallbacks = [];

      const blocks = Array.from(node.querySelectorAll('pre'));
      for (const [index, block] of blocks.entries()) {
        const code = block.querySelector('code');
        if (!code) continue;

        block.classList.add('chat-code-block');

        const button = document.createElement('button');
        const label = document.createElement('span');
        const copyKey = `${item.id}:${index}`;
        label.textContent = copiedCodeBlocks.has(copyKey) ? 'Copied' : 'Copy';
        button.type = 'button';
        button.className = 'chat-code-copy-button';
        button.setAttribute('aria-label', 'Copy code block');
        button.innerHTML = copiedCodeBlocks.has(copyKey)
          ? '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M20 6 9 17l-5-5"></path></svg>'
          : '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="9" y="9" width="13" height="13" rx="2" ry="2"></rect><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"></path></svg>';
        button.append(label);

        const onClick = async (): Promise<void> => {
          try {
            await navigator.clipboard.writeText(code.textContent ?? '');
            copiedCodeBlocks = new Set([...copiedCodeBlocks, copyKey]);
            const existingTimer = codeCopyResetTimers.get(copyKey);
            if (existingTimer !== undefined) {
              window.clearTimeout(existingTimer);
            }
            renderButtons();
            const resetTimer = window.setTimeout(() => {
              const next = new Set(copiedCodeBlocks);
              next.delete(copyKey);
              copiedCodeBlocks = next;
              codeCopyResetTimers.delete(copyKey);
              renderButtons();
            }, 2000);
            codeCopyResetTimers.set(copyKey, resetTimer);
          } catch {
            addToast('Failed to copy code block', 'error');
          }
        };

        button.addEventListener('click', onClick);
        block.append(button);
        cleanupCallbacks.push(() => button.removeEventListener('click', onClick));
        cleanupCallbacks.push(() => button.remove());
      }
    };

    renderButtons();

    return {
      update(_nextHtml: string) {
        renderButtons();
      },
      destroy() {
        cleanupCallbacks.forEach((cleanup) => cleanup());
        cleanupCallbacks = [];
      }
    };
  }
</script>

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

  <div class="mt-2.5 flex items-center justify-between gap-3 text-[10px] uppercase tracking-[0.18em] opacity-70 sm:mt-3 sm:text-[11px]">
    <div class="flex items-center gap-2 sm:gap-3">
      <span>{item.role}</span>
      <span title={formatAbsoluteTime(item.timestamp)}>{formatCompactTime(item.timestamp, now)}</span>
    </div>
    <div class="flex items-center gap-2">
      {#if item.streaming}
        <LiveDots inline={true} label="Live" size="sm" tone={item.role === 'assistant' ? 'sky' : 'slate'} />
      {/if}
      {#if item.role === 'assistant' && !item.streaming}
        <button
          class="inline-flex h-7 w-7 items-center justify-center rounded-full text-slate-400 transition hover:bg-slate-800/80 hover:text-slate-100"
          onclick={copyMessage}
          type="button"
          title="Copy raw markdown"
          aria-label="Copy raw markdown"
        >
          {#if messageCopied}
            <Check class="h-3.5 w-3.5" />
          {:else}
            <Copy class="h-3.5 w-3.5" />
          {/if}
        </button>
      {/if}
    </div>
  </div>
</article>
