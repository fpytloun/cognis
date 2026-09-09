<script lang="ts">
  let {
    title,
    text = '',
    details = '',
    tone = 'info',
    active = false,
    actionRequired = false,
    code = null
  } = $props<{
    title: string;
    text?: string;
    details?: string;
    tone?: 'info' | 'warning' | 'error';
    active?: boolean;
    actionRequired?: boolean;
    code?: string | null;
  }>();

  const palette = $derived(
    tone === 'error'
      ? 'border-rose-500/35 bg-rose-500/10 text-rose-100'
      : tone === 'warning'
        ? 'border-amber-400/30 bg-amber-500/10 text-amber-100'
        : 'border-slate-700/80 bg-slate-900/70 text-slate-300'
  );
  const dot = $derived(
    tone === 'error' ? 'bg-rose-300' : tone === 'warning' ? 'bg-amber-300' : 'bg-slate-400'
  );
  const summary = $derived.by(() => {
    const normalized = text.trim().replace(/\s+/g, ' ');
    if (normalized === title.trim()) return '';
    return normalized;
  });
  const fullDetails = $derived(details.trim() || text.trim());
  const normalizedDetails = $derived(fullDetails.replace(/\s+/g, ' '));
  const displayedText = $derived(summary || title.trim().replace(/\s+/g, ' '));
  const showDetails = $derived(
    Boolean(fullDetails) && (normalizedDetails !== displayedText || Boolean(code))
  );
</script>

<article
  class={`mx-auto w-full border text-xs ${palette} ${actionRequired || tone === 'error' ? 'max-w-2xl rounded-2xl px-4 py-3 shadow-card' : 'max-w-2xl rounded-xl px-3 py-2'}`}
  role={tone === 'error' ? 'alert' : active ? 'status' : undefined}
>
  <div class="flex items-start gap-2.5">
    <span class={`mt-1.5 h-2 w-2 shrink-0 rounded-full ${dot} ${active ? 'animate-pulse' : ''}`} aria-hidden="true"></span>
    <div class="min-w-0 flex-1">
      <div class="flex flex-wrap items-center gap-2">
        <p class="font-semibold">{title}</p>
        {#if code}
          <code class="rounded bg-black/20 px-1.5 py-0.5 text-[11px]">{code}</code>
        {/if}
      </div>
      {#if summary}
        <p class="mt-1 whitespace-pre-line leading-5 opacity-90">{summary}</p>
      {/if}
      {#if showDetails}
        <details class="mt-2">
          <summary class="cursor-pointer select-none font-medium opacity-75">Details</summary>
          <pre class="mt-2 max-h-80 overflow-auto whitespace-pre-wrap break-words rounded-lg bg-black/20 p-3 font-mono text-[11px] leading-5">{fullDetails}</pre>
        </details>
      {/if}
    </div>
  </div>
</article>
