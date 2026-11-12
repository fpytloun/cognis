<script lang="ts">
  import type { FeedbackLocator, FeedbackSurfaceBinding, FeedbackTextRange, FeedbackTextScope, ScopedCanonicalFeedbackText } from '$lib/feedback/types';
  import { selectText } from '$lib/feedback/annotations/text';
  import { surfaceIdentity } from '$lib/feedback/annotations/surface-identity';
  import { untrack } from 'svelte';
  let { binding, scope, locator }: { binding: FeedbackSurfaceBinding; scope: FeedbackTextScope; locator: (range: FeedbackTextRange) => FeedbackLocator } = $props();
  let result = $state<ScopedCanonicalFeedbackText | null>(null), error = $state(''), busy = $state(false);
  let input = $state<HTMLTextAreaElement>(), selected = $state(false);
  let request = 0;
  export function clearSelection() {
    request++; selected = false; busy = false;
    input?.setSelectionRange(0, 0);
    binding.onInteractionChange(false);
  }
  const scopeKey = $derived(JSON.stringify(scope));
  const identityKey = $derived(surfaceIdentity(binding));
  $effect(() => { identityKey; scopeKey; const current = untrack(() => binding); result = null; selected = false; busy = false; request++; return () => { request++; current.onInteractionChange(false); }; });
  async function load(start = 0) {
    const generation = ++request, current = binding, token = surfaceIdentity(current);
    busy = true; error = ''; current.onInteractionChange(true);
    try {
      const value = await current.resolveTextScope(scope, { start, limit: 16384 });
      if (request !== generation || surfaceIdentity(binding) !== token) return;
      result = value; selected = false;
    } catch (cause) { if (request === generation) error = (cause as Error).message; }
    finally { if (request === generation) busy = false; }
  }
  function submit() {
    if (!result || !input) return;
    try {
      const { text_page: page, descriptor } = result;
      const range = selectText(page.text, input.selectionStart, input.selectionEnd);
      const start = page.start + range.start, end = page.start + range.end;
      if (!descriptor.available_ranges.some(([left, right]) => left <= start && end <= right)) throw new Error('This range is not available for annotation.');
      binding.onAnchorCandidate({ locator: locator({ representation: 'feedback-text-v1', text_digest: descriptor.text_digest,
        exact: range.exact, prefix: range.prefix, suffix: range.suffix, start, end }) });
      selected = false; binding.onInteractionChange(false);
    } catch (cause) { error = (cause as Error).message; }
  }
</script>
<div class="max-w-full p-2 text-sm">
  <button class="min-h-11 px-2" disabled={busy} onclick={() => void load(result?.text_page.start ?? 0)}>{busy ? 'Loading canonical text…' : 'Load canonical text for selection'}</button>
  {#if error}<p role="alert">{error}</p>{/if}
  {#if result}
    <textarea bind:this={input} readonly value={result.text_page.text} aria-label="Canonical source text"
      class="min-h-32 w-full bg-inset p-2 font-mono" onselect={() => { selected = !!input && input.selectionStart !== input.selectionEnd; binding.onInteractionChange(selected); }}></textarea>
    <button class="min-h-11 px-2" disabled={!selected || !binding.source.capabilities.comment} onclick={submit}>Comment on selected text</button>
    <button class="min-h-11 px-2" disabled={busy || result.text_page.start === 0} onclick={() => void load(Math.max(0, result!.text_page.start - 16384))}>Previous text</button>
    <button class="min-h-11 px-2" disabled={busy || result.text_page.end >= result.text_page.total_length} onclick={() => void load(result!.text_page.end)}>Next text</button>
    <span>{result.text_page.start}–{result.text_page.end} of {result.text_page.total_length} UTF-16 units</span>
  {/if}
</div>
