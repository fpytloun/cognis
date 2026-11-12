<script lang="ts">
  import { getFeedbackSurface } from '$lib/feedback/context';
  import { canonicalText, domTextRange, looseQuoteRange, selectText, rangeOffsets } from '$lib/feedback/annotations/text';
  import { textDigest } from '$lib/feedback/annotations/digest';
  import { nativeSelection, rangeGeometry, clearNativeSelection } from '$lib/feedback/annotations/native-selection';
  import type { FeedbackLocator } from '$lib/feedback/types';
  import { exposeGeometry } from '$lib/feedback/annotations/expose-target';
  import type { Snippet } from 'svelte';
  import ScopedTextSelection from './ScopedTextSelection.svelte';
  import TextHighlights from './TextHighlights.svelte';
  let { text, children }: { text: string; children: Snippet } = $props();
  const surface = getFeedbackSurface();
  const binding = $derived(surface?.binding);
  let root = $state<HTMLDivElement>(), error = $state('');
  const instance = crypto.randomUUID();
  const normalized = $derived(canonicalText(text));
  async function map(range: Range) {
    const current = binding;
    if (!current?.source.canonical_text || !root) throw new Error('Canonical text is unavailable for this source.');
    const descriptor = current.source.canonical_text;
    if (descriptor.text_digest !== await textDigest(normalized) || descriptor.length !== normalized.length) throw new Error('Displayed source does not match the exact canonical revision.');
    const exact = canonicalText(range.toString());
    let position: { start: number; end: number } | null;
    if (canonicalText(root.textContent ?? '') === normalized) {
      position = rangeOffsets(root, range);
      if (position && normalized.slice(position.start, position.end) !== exact) position = null;
    } else {
      // A reformatted view (for example indented JSON) differs only in whitespace; the rendered
      // text before the selection disambiguates repeated quotes.
      const before = root.ownerDocument.createRange();
      before.setStart(root, 0); before.setEnd(range.startContainer, range.startOffset);
      position = looseQuoteRange(normalized, exact, before.toString(), true);
    }
    if (!position) throw new Error('This rendered range is ambiguous. Select a precise range in the source view.');
    const selected = selectText(normalized, position.start, position.end, descriptor.available_ranges.map(([start, end]) => ({ start, end })));
    return { candidate: { locator: { kind: 'text' as const, range: { representation: 'feedback-text-v1' as const, text_digest: descriptor.text_digest,
      ...selected } } }, label: selected.exact };
  }
  function rangeFor(anchor: FeedbackLocator) {
    if (anchor.kind !== 'text' || !root) return null;
    const displayed = canonicalText(root.textContent ?? '');
    if (displayed === normalized) {
      if (displayed.slice(anchor.range.start, anchor.range.end) !== anchor.range.exact) return null;
      return domTextRange(root, anchor.range.start, anchor.range.end);
    }
    const position = looseQuoteRange(displayed, anchor.range.exact, anchor.range.prefix, true);
    return position ? domTextRange(root, position.start, position.end) : null;
  }
  $effect(() => {
    if (!binding) return;
    return binding.registerAdapter(instance, {
      clearSelection: () => clearNativeSelection(root),
      targetGeometry: anchor => { const range = rangeFor(anchor); return range ? rangeGeometry(range) : null; },
      exposeTarget: (anchor, visible) => exposeGeometry(root, () => { const range = rangeFor(anchor); return range ? rangeGeometry(range) : null; }, visible),
      locate: anchor => {
        const range = rangeFor(anchor);
        if (!range) return { status: 'not_located' };
        range.startContainer.parentElement?.scrollIntoView({ block: 'center' }); return { status: 'exact' };
      },
    });
  });
</script>
<div>
  {#if error}<p role="alert" class="p-2 text-sm text-danger">{error}</p>{/if}
  <div class="relative">
    <div bind:this={root} use:nativeSelection={{ binding, instance, map, error: message => { error = message; } }}>{@render children()}</div>
    {#if binding?.mode !== 'view'}<TextHighlights {root} highlights={binding?.highlights ?? []} />{/if}
  </div>
  {#if error && binding?.source.capabilities.comment}<details><summary class="min-h-11 cursor-pointer p-2 text-xs">Select canonical source text instead</summary><ScopedTextSelection {binding} scope={{ kind: 'document' }} locator={range => ({ kind: 'text', range })} /></details>{/if}
</div>
