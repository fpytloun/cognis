<script lang="ts">
  import type { FeedbackHighlight } from '$lib/feedback/types';
  import { domTextRange, mapUniqueQuote } from '$lib/feedback/annotations/text';
  import { paintTextRanges, unionIntervals } from '$lib/feedback/text-paint';
  import { unionRects, type TargetRect } from '$lib/feedback/floating-layout';
  let { root, highlights }: { root: HTMLElement | undefined; highlights: FeedbackHighlight[] } = $props();
  let boxes = $state<TargetRect[]>([]);
  $effect(() => {
    const node = root, values = highlights.filter(highlight => highlight.category !== 'not_located' && highlight.locator.kind === 'text');
    if (!node || !values.length) { boxes = []; return; }
    let removePaint: (() => void) | null = null, queued = 0;
    function update() {
      removePaint?.(); removePaint = null;
      const text = node!.textContent ?? '', bounds = node!.getBoundingClientRect();
      const intervals = values.flatMap(highlight => {
        if (highlight.category === 'not_located' || highlight.locator.kind !== 'text') return [];
        const quote = highlight.locator.range;
        const position = text.slice(quote.start, quote.end) === quote.exact
          ? { start: quote.start, end: quote.end } : mapUniqueQuote(text, quote);
        return position ? [position] : [];
      });
      const ranges = unionIntervals(intervals).flatMap(position => {
        const range = domTextRange(node!, position.start, position.end); return range ? [range] : [];
      });
      removePaint = paintTextRanges(node!, ranges);
      boxes = removePaint ? [] : unionRects(ranges.flatMap(range => range.getClientRects
        ? Array.from(range.getClientRects(), rect => ({ left: rect.left - bounds.left, top: rect.top - bounds.top, width: rect.width, height: rect.height })) : []));
    }
    function schedule() { if (!queued) queued = requestAnimationFrame(() => { queued = 0; update(); }); }
    const resize = new ResizeObserver(schedule); resize.observe(node);
    const mutations = new MutationObserver(() => { removePaint?.(); removePaint = null; schedule(); });
    mutations.observe(node, { childList: true, characterData: true, subtree: true });
    window.addEventListener('scroll', schedule, true); update();
    return () => { removePaint?.(); cancelAnimationFrame(queued); resize.disconnect(); mutations.disconnect(); window.removeEventListener('scroll', schedule, true); };
  });
</script>
<div aria-hidden="true" class="pointer-events-none absolute inset-0">
  {#each boxes as box}<span data-feedback-passive-highlight class="absolute outline outline-1 outline-accent"
    style:left={`${box.left}px`} style:top={`${box.top}px`} style:width={`${box.width}px`} style:height={`${box.height}px`}></span>{/each}
</div>
