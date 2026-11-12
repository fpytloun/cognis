<script lang="ts">
  import { onDestroy, untrack } from 'svelte';
  import { Volume1, Volume2, LoaderCircle, X } from 'lucide-svelte';
  import { defaultDocumentAudioClient, type DocumentAudioClient } from '$lib/document-audio';
  import { portal } from '$lib/actions/portal';
  import { registerOverlay, isTopOverlay } from '$lib/stores/overlays';
  import { audioPlayer } from '$lib/stores/audio-player';
  import type { AudioJob, PodcastLengthEstimate } from '$lib/types/api';
  import DeferredView from './DeferredView.svelte';
  import './rich/document-actions.css';

  let { sourceRef, client = defaultDocumentAudioClient }: {
    sourceRef: string; rich?: boolean; client?: DocumentAudioClient;
  } = $props();
  let open = $state(false);
  let jobs = $state<AudioJob[]>([]);
  let busy = $state(false);
  let error = $state('');
  let canGenerate = $state(false);
  let canEstimate = $state(false);
  let canCancel = $state(false);
  let supported = $state(true);
  let detail = $state<string | null>(null);
  let trigger = $state<HTMLButtonElement | null>(null);
  let panel = $state<HTMLDivElement | null>(null);
  let floatingStyle = $state('');
  let overlay: ReturnType<typeof registerOverlay> | null = null;
  const lengthGroup = $props.id();
  let length = $state<'shorter' | 'default' | 'longer'>('default');
  let estimates = $state<PodcastLengthEstimate[] | null>(null);
  let estimating = $state(false);
  let estimateError = $state('');
  let estimateController: AbortController | null = null;
  let mode = $state<'narration' | 'podcast'>('narration');
  let modeChosen = false;
  let requestKey: string | null = null;
  let requestMode: string | null = null;
  let timer: ReturnType<typeof setTimeout> | undefined;
  let disposed = false;
  let controller: AbortController | null = null;
  let generation = 0;
  const active = (job: AudioJob) => ['queued', 'scripting', 'synthesizing'].includes(job.status);
  const ordered = $derived([...jobs].sort((a, b) => Date.parse(b.created_at) - Date.parse(a.created_at)));
  const current = $derived(ordered.find(job => job.mode === mode));
  const recording = $derived(ordered.find(job =>
    job.mode === mode && job.parts.length && (job.status === 'completed' || active(job))));
  const generating = $derived(jobs.some(active));
  const ownsPlayback = $derived(jobs.some(job => job.job_id === $audioPlayer.currentKey));
  const playbackState = $derived(ownsPlayback ? ($audioPlayer.isPlaying ? 'playing' : 'paused') : generating ? 'generating' : 'idle');
  const playbackTitle = $derived(playbackState === 'idle' ? 'Listen to this document' : `Document audio · ${playbackState}`);
  const failureDetails: Record<string, string> = {
    source_unavailable: 'The source is no longer accessible.',
    script_empty: 'The writing model returned no spoken text. Check the audio narrative model.',
    script_truncated: 'The script reached the model output limit. Try a shorter podcast or a model with a larger output limit.',
    script_incomplete: 'The writing model did not finish a spoken script. Check the audio narrative model.',
    script_too_long: 'The script exceeded the requested length. Try a shorter podcast.',
    speech_unavailable: 'No usable speech model or voice is configured.',
    generation_internal_error: 'An internal audio-generation error occurred. This is a Cognis issue, not a model settings problem.',
    provider_access_denied: 'The model provider refused access. Check the API key and model or voice permissions.',
    provider_quota_exhausted: 'The model provider reports insufficient quota or credits. Check the provider usage limits.',
    provider_rate_limited: 'The model provider is rate-limiting requests. Wait before trying again.',
    provider_request_rejected: 'The model provider rejected this request. Check the selected model and voice.',
    provider_unavailable: 'The model provider could not be reached or did not respond in time.',
    speech_stream_incomplete: 'The speech provider did not complete its audio stream. The incomplete part was not saved.',
  };
  $effect(() => {
    void sourceRef;
    untrack(() => {
      generation += 1;
      clearTimeout(timer);
      controller?.abort();
      estimateController?.abort();
      estimateController = null;
      estimates = null;
      estimating = false;
      estimateError = '';
      length = 'default';
      jobs = [];
      mode = 'narration';
      modeChosen = false;
      requestKey = null;
      requestMode = null;
      busy = false;
      error = '';
      canGenerate = false;
      canEstimate = false;
      canCancel = false;
      detail = null;
      if (open) void refresh();
    });
  });
  $effect(() => {
    if (open && mode === 'podcast' && supported && canEstimate && !estimates && !estimating && !estimateError) {
      void loadEstimates();
    }
  });

  async function loadEstimates() {
    estimateController?.abort();
    const request = new AbortController();
    estimateController = request;
    const admitted = generation;
    estimating = true;
    estimateError = '';
    try {
      const values = await client.estimates(sourceRef, { signal: request.signal });
      if (disposed || request.signal.aborted || admitted !== generation) return;
      if (!['shorter', 'default', 'longer'].every(value => values.some(plan => plan.length === value))) {
        throw new Error('Length estimates are unavailable.');
      }
      estimates = values;
    } catch {
      if (!disposed && !request.signal.aborted && admitted === generation) {
        estimateError = 'Length estimates are unavailable. Try again.';
      }
    } finally {
      if (estimateController === request) {
        estimateController = null;
        estimating = false;
      }
    }
  }

  function estimateLabel(value: string): string {
    const plan = estimates?.find(plan => plan.length === value);
    if (!plan) return estimating ? 'Estimating…' : '—';
    if (plan.seconds_max < 60) return '<1 min';
    const lower = Math.max(1, Math.floor(plan.seconds_min / 60));
    const upper = Math.max(lower, Math.ceil(plan.seconds_max / 60));
    return lower === upper ? `~${lower} min` : `~${lower}–${upper} min`;
  }

  async function refresh() {
    clearTimeout(timer);
    controller?.abort();
    const request = new AbortController();
    controller = request;
    const admitted = generation;
    try {
      const context = await client.context(sourceRef, { signal: request.signal });
      if (disposed || request.signal.aborted || admitted !== generation) return;
      const values = context.jobs;
      jobs = values;
      if (!modeChosen && values.length) {
        mode = values.find(active)?.mode ?? [...values].sort((a, b) => Date.parse(b.created_at) - Date.parse(a.created_at))[0].mode;
        modeChosen = true;
      }
      canGenerate = context.can_generate;
      canEstimate = context.can_estimate ?? context.can_generate;
      canCancel = context.can_cancel;
      supported = context.supported;
      detail = context.detail;
      error = '';
      if (open && values.some(active)) timer = setTimeout(() => void refresh(), 3000);
    } catch (caught) {
      if (!disposed && !request.signal.aborted && admitted === generation) error = caught instanceof Error ? caught.message : 'Unable to load audio. Try reopening Listen.';
    }
  }
  async function generate(mode: 'narration' | 'podcast') {
    if (busy || generating || !canGenerate || !supported || (mode === 'podcast' && !estimates)) return;
    const admitted = generation;
    busy = true;
    error = '';
    // A transport failure keeps the same key: retry cannot duplicate paid work.
    const signature = `${sourceRef}:${mode}:${mode === 'podcast' ? length : ''}`;
    if (!requestKey || requestMode !== signature) {
      requestKey = crypto.randomUUID();
      requestMode = signature;
    }
    try {
      const job = await client.create({
        content_ref: sourceRef, mode, idempotency_key: requestKey,
        ...(mode === 'podcast' ? { length } : {})
      });
      if (disposed || admitted !== generation) return;
      mode = job.mode;
      modeChosen = true;
      requestKey = null;
      await refresh();
    } catch (caught) {
      if (!disposed && admitted === generation) error = caught instanceof Error ? caught.message : 'Audio generation could not start.';
    } finally { if (admitted === generation) busy = false; }
  }
  async function cancel() {
    if (!current || !canCancel || busy) return;
    const admitted = generation;
    busy = true;
    try {
      await client.cancel(current.job_id);
      if (disposed || admitted !== generation) return;
      await refresh();
    } catch (caught) {
      if (!disposed && admitted === generation) error = caught instanceof Error ? caught.message : 'Cancellation failed. Try again.';
    } finally { if (admitted === generation) busy = false; }
  }
  onDestroy(() => {
    disposed = true;
    clearTimeout(timer);
    controller?.abort();
    estimateController?.abort();
    overlay?.unregister();
  });
  $effect(() => {
    if (!open || !panel || typeof ResizeObserver === 'undefined') return;
    const observer = new ResizeObserver(position);
    observer.observe(panel);
    return () => observer.disconnect();
  });
  $effect(() => {
    if (!open) return;
    window.addEventListener('scroll', position, true);
    return () => window.removeEventListener('scroll', position, true);
  });
  function chooseMode(value: 'narration' | 'podcast') {
    mode = value;
    modeChosen = true;
  }
  function tabKey(event: KeyboardEvent) {
    if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
    event.preventDefault();
    chooseMode(event.key === 'Home' ? 'narration' : event.key === 'End' ? 'podcast' : mode === 'narration' ? 'podcast' : 'narration');
    panel?.querySelector<HTMLButtonElement>(`[role="tab"][data-mode="${mode}"]`)?.focus();
  }
  function position() {
    if (!trigger || !open) return;
    const rect = trigger.getBoundingClientRect();
    const width = Math.min(380, window.innerWidth - 32);
    const left = Math.max(16, Math.min(rect.right - width, window.innerWidth - width - 16));
    const top = Math.max(16, Math.min(rect.bottom + 8, window.innerHeight - (panel?.offsetHeight || 300) - 16));
    const theme = getComputedStyle(trigger);
    const color = (richToken: string, appToken: string, fallback: string) =>
      theme.getPropertyValue(richToken).trim() || `rgb(${theme.getPropertyValue(appToken).trim() || fallback})`;
    // App color tokens are RGB channels, not CSS colors. Never copy a transparent
    // toolbar button's computed background into the portaled reading surface.
    floatingStyle = `position:fixed;left:${left}px;top:${top}px;width:${width}px;max-height:${window.innerHeight - top - 16}px;overflow-y:auto;--audio-bg:${color('--rich-surface-raised', '--color-elevated', '24 32 48')};--audio-text:${color('--rich-text', '--color-foreground', '229 231 235')};--audio-muted:${color('--rich-text-secondary', '--color-secondary', '156 163 175')};--audio-accent:${color('--rich-accent', '--color-accent', '147 197 253')};--audio-line:${color('--rich-line', '--color-separator', '100 116 139')};`;
  }
  function toggle() {
    if (open) { close(); return; }
    open = true;
    // A listening popover is not modal: removing the page scrollbar shifts the document.
    overlay = registerOverlay({ kind: 'sheet', blocksChrome: false, locksScroll: false, root: () => panel });
    position(); void refresh();
  }
  function close() {
    open = false; clearTimeout(timer); controller?.abort(); estimateController?.abort();
    overlay?.unregister(); overlay = null;
  }
  function outside(event: PointerEvent) {
    if (event.target instanceof Node && !trigger?.contains(event.target) && !panel?.contains(event.target)) {
      close();
    }
  }
</script>

<svelte:window onpointerdown={outside} onresize={position} onkeydown={(event) => {
  if (open && event.key === 'Escape' && isTopOverlay(overlay?.id ?? null)) {
    event.preventDefault(); event.stopPropagation(); close(); trigger?.focus({ preventScroll: true });
  }
}} />
  <button type="button" bind:this={trigger} onclick={toggle} aria-label="Listen to this document"
    aria-haspopup="dialog" aria-expanded={open} title={playbackTitle} data-playback={playbackState}
    class="document-audio-trigger document-icon-action">
    {#if playbackState === 'generating'}<LoaderCircle aria-hidden="true" />
    {:else if playbackState === 'playing'}<Volume2 aria-hidden="true" />
    {:else}<Volume1 aria-hidden="true" />{/if}
    {#if playbackState === 'playing' || playbackState === 'paused'}<span class="playback-dot" aria-hidden="true"></span>{/if}
  </button>
    <!-- Stays mounted so playback continues while closed; only an open panel is a dialog. -->
    <div bind:this={panel} use:portal hidden={!open} style={floatingStyle} role={open ? "dialog" : undefined}
      aria-label="Document audio" tabindex="-1"
      class="document-audio-panel" data-testid="deliverable-audio">
      <header>
        <div class="audio-tabs" role="tablist" aria-label="Audio format">
          {#each ['narration', 'podcast'] as value}
            <button type="button" role="tab" data-mode={value} aria-selected={mode === value}
              tabindex={mode === value ? 0 : -1} onkeydown={tabKey}
              onclick={() => chooseMode(value as 'narration' | 'podcast')}>{value === 'podcast' ? 'Podcast' : 'Reading'}</button>
          {/each}
        </div>
        <button class="audio-icon" type="button" aria-label="Close audio controls" title="Close" onclick={close}><X /></button>
      </header>
      {#if detail}<p role="status" class="text-xs text-muted">{detail}</p>{/if}
      {#if recording}
        {#key recording.job_id}
          {@const selectedRecording = recording}
          <DeferredView load={() => import('./AudioPlaylist.svelte')} label="Loading audio controls…">
            {#snippet children(loaded)}
              <loaded.default parts={selectedRecording.parts} playbackKey={selectedRecording.job_id}
                complete={!active(selectedRecording)} rich />
            {/snippet}
          </DeferredView>
        {/key}
      {:else if !current || !active(current)}
        <p class="audio-muted">{mode === 'podcast' ? 'Create a source-grounded podcast from this revision.' : 'Read this revision aloud.'}</p>
      {/if}
      {#if current}
        {#if active(current)}
          <div class="audio-generation-status">
            <span role="status">{current.status === 'scripting' ? 'Preparing podcast' : current.status === 'synthesizing' ? `Generating audio ${current.completed_parts} / ${current.total_parts}` : 'Queued'}</span>
            <button type="button" disabled={busy || !canCancel} onclick={() => void cancel()}>Cancel generation</button>
          </div>
        {/if}
        {#if ['failed', 'interrupted'].includes(current.status)}
          <p role="alert">Generation did not finish. {failureDetails[current.error_code ?? ''] ?? 'Check the speech and writing models, then try again.'} No automatic retries.</p>
        {/if}
        {#each current.warnings ?? [] as warning}<p role="status" class="text-xs text-muted">{warning}</p>{/each}
      {/if}
      {#if mode === 'podcast'}
        <fieldset class="audio-lengths" disabled={busy || generating || !canGenerate}>
          <legend>Length</legend>
          <div>
            {#each ['shorter', 'default', 'longer'] as value}
              <label>
                <input type="radio" name={lengthGroup} value={value} bind:group={length} />
                <span>{value === 'shorter' ? 'Shorter' : value === 'longer' ? 'Longer' : 'Default'}</span>
                <small>{estimateLabel(value)}</small>
              </label>
            {/each}
          </div>
        </fieldset>
        <p class="audio-muted">Estimates vary with the material and speaking pace.</p>
        {#if estimateError}
          <p role="status" class="audio-muted">{estimateError} <button type="button" onclick={() => void loadEstimates()}>Retry estimates</button></p>
        {/if}
      {/if}
      <div class="audio-generate">
        <button type="button" disabled={busy || generating || !canGenerate || !supported || (mode === 'podcast' && !estimates)} onclick={() => void generate(mode)}>
          {recording ? 'Generate again' : mode === 'podcast' ? 'Generate podcast' : 'Generate narration'}
        </button>
        <span class="audio-muted">May incur charges</span>
      </div>
      {#if error}<p role="alert" class="text-xs text-danger">{error}</p>{/if}
    </div>

<style>
  /* Standalone documents do not load the application Tailwind stylesheet. */
  .document-audio-trigger { position: relative; }
  .document-audio-trigger[data-playback="playing"] {
    color: var(--rich-accent, rgb(var(--color-accent, 147 197 253)));
    border-color: currentColor; background: var(--rich-surface-raised, rgb(var(--color-elevated, 24 32 48)));
  }
  .playback-dot { position: absolute; right: 5px; bottom: 5px; width: 5px; height: 5px; border: 1px solid currentColor; border-radius: 50%; }
  [data-playback="playing"] .playback-dot { background: currentColor; }
  .document-audio-panel {
    box-sizing: border-box; z-index: 2147483646; padding: .75rem; border: 1px solid var(--audio-line, #64748b); border-radius: .75rem;
    background: var(--audio-bg, #182030); color: var(--audio-text, #e5e7eb);
    box-shadow: 0 12px 40px #0006; font: .8125rem/1.45 ui-sans-serif, system-ui, sans-serif;
  }
  .document-audio-panel > * + * { margin-top: .65rem; }
  .document-audio-panel p { margin-bottom: 0; font-size: .75rem; }
  header, .audio-tabs, .audio-generation-status, .audio-generate { display: flex; align-items: center; gap: .4rem; }
  header { margin-bottom: .5rem; }
  .audio-tabs { margin-right: auto; }
  .document-audio-panel .audio-tabs button { border-color: transparent; background: transparent; }
  .audio-tabs button[aria-selected="true"] { color: var(--audio-accent, #93c5fd); box-shadow: inset 0 -2px currentColor; }
  .audio-muted { color: var(--audio-muted, inherit); font-size: .7rem; }
  .audio-lengths { min-width: 0; margin: 0; padding: 0; border: 0; }
  .audio-lengths legend { margin-bottom: .35rem; font-size: .75rem; }
  .audio-lengths > div { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: .35rem; }
  .audio-lengths label { position: relative; display: grid; justify-items: center; gap: .2rem; padding: .5rem .2rem; border: 1px solid var(--audio-line, #64748b); border-radius: .45rem; cursor: pointer; }
  .audio-lengths label:has(:checked) { border-color: var(--audio-accent, #93c5fd); background: color-mix(in srgb, var(--audio-accent, #93c5fd) 10%, var(--audio-bg, #182030)); }
  .audio-lengths input { position: absolute; opacity: 0; width: 1px; height: 1px; }
  .audio-lengths label:has(:focus-visible) { outline: 2px solid var(--audio-accent, #93c5fd); outline-offset: 2px; }
  .audio-lengths small { font-size: .65rem; color: var(--audio-muted, inherit); white-space: nowrap; }
  .audio-lengths:disabled { opacity: .5; }
  .document-audio-panel button {
    min-height: 2.25rem; padding: .35rem .5rem; border: 1px solid var(--audio-line, #64748b); border-radius: .45rem;
    background: var(--audio-bg, #182030); color: inherit; font: inherit;
  }
  .document-audio-panel button { cursor: pointer; }
  .document-audio-panel button:disabled { opacity: .5; cursor: not-allowed; }
  .document-audio-panel .audio-icon { display: inline-grid; place-items: center; min-height: 2.25rem; width: 2.25rem; padding: 0; border: 0; color: inherit; }
  .audio-icon :global(svg) { width: 1rem; height: 1rem; }
  .audio-generate { flex-wrap: wrap; border-top: 1px solid var(--audio-line, #64748b); padding-top: .65rem; }
  .audio-generation-status { justify-content: space-between; font-size: .75rem; }
  .document-audio-panel [role="alert"] { color: #f87171; }
  .document-audio-panel button:focus-visible { outline: 2px solid var(--audio-accent, #93c5fd); outline-offset: 2px; }
  @media (pointer: coarse) {
    .document-audio-panel button, .document-audio-panel .audio-icon { min-height: 2.75rem; }
    .document-audio-panel .audio-icon { width: 2.75rem; }
  }
</style>
