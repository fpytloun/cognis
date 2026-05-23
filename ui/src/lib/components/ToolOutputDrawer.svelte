<script lang="ts">
  import { api } from '$lib/api/client';
  import { renderTerminalOutput } from '$lib/syntax/terminal-output';
  import type { ToolOutputPageResponse } from '$lib/types/api';

  let {
    open,
    conversationId,
    sessionId = null,
    callId,
    toolName,
    isTerminal = false,
    onClose,
  } = $props<{
    open: boolean;
    conversationId?: string | null;
    sessionId?: string | null;
    callId: string;
    toolName: string;
    isTerminal?: boolean;
    onClose: () => void;
  }>();

  let page = $state<ToolOutputPageResponse | null>(null);
  let loading = $state(false);
  let error = $state<string | null>(null);
  let content = $state('');

  $effect(() => {
    if (open && conversationId && callId) {
      void load({ latest: true, replace: true });
    }
  });

  async function load(opts: { offset?: number | null; latest?: boolean; prepend?: boolean; replace?: boolean } = {}): Promise<void> {
    if (!conversationId) return;
    loading = true;
    error = null;
    try {
      const next = await api.conversations.toolOutputPage(conversationId, callId, {
        sessionId,
        offset: opts.offset ?? undefined,
        limit: 200,
        latest: opts.latest,
      });
      page = next;
      if (opts.prepend) content = `${next.content}${content ? `\n${content}` : ''}`;
      else if (opts.replace) content = next.content;
      else content = `${content}${content && next.content ? '\n' : ''}${next.content}`;
    } catch (err) {
      error = err instanceof Error ? err.message : 'Failed to load tool output';
    } finally {
      loading = false;
    }
  }
</script>

{#if open}
  <div class="fixed inset-0 z-50 bg-slate-950/70 backdrop-blur-sm" role="presentation" onclick={onClose}></div>
  <aside class="fixed right-0 top-0 z-50 flex h-full w-full max-w-4xl flex-col border-l border-slate-700 bg-slate-950 text-slate-100 shadow-2xl" aria-label="Tool output drawer">
    <header class="flex items-center justify-between border-b border-slate-800 px-4 py-3">
      <div>
        <p class="text-sm font-semibold">{toolName}</p>
        <p class="text-xs text-slate-400">{callId}{#if page} · {page.source} · {page.status}{/if}</p>
      </div>
      <button class="rounded-lg border border-slate-700 px-3 py-1 text-sm text-slate-200 hover:bg-slate-800" type="button" onclick={onClose}>Close</button>
    </header>
    <div class="flex gap-2 border-b border-slate-800 px-4 py-2 text-xs">
      <button class="rounded border border-slate-700 px-2 py-1 disabled:opacity-40" type="button" disabled={loading || !page?.has_more_before} onclick={() => void load({ offset: page?.prev_offset, prepend: true })}>Load earlier</button>
      <button class="rounded border border-slate-700 px-2 py-1 disabled:opacity-40" type="button" disabled={loading || !page?.has_more_after} onclick={() => void load({ offset: page?.next_offset })}>Load later</button>
      <button class="rounded border border-slate-700 px-2 py-1 disabled:opacity-40" type="button" disabled={loading} onclick={() => void load({ latest: true, replace: true })}>Jump to latest</button>
      {#if page?.output_size}<span class="ml-auto text-slate-500">{page.output_size.toLocaleString()} bytes</span>{/if}
    </div>
    {#if error}
      <p class="m-4 rounded border border-rose-500/30 bg-rose-500/10 p-3 text-sm text-rose-100">{error}</p>
    {/if}
    <div class="min-h-0 flex-1 overflow-auto p-4">
      {#if isTerminal}
        <pre class="min-h-full whitespace-pre-wrap rounded-xl bg-black p-3 font-mono text-xs leading-5 text-emerald-100">{@html renderTerminalOutput(content || 'No output available.')}</pre>
      {:else}
        <pre class="min-h-full whitespace-pre-wrap rounded-xl border border-slate-800 bg-slate-900/70 p-3 font-mono text-xs leading-5 text-slate-200">{content || 'No output available.'}</pre>
      {/if}
    </div>
  </aside>
{/if}

