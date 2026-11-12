<script lang="ts" module>
  /** One picked area per document: a new pick in any block clears the previous one. */
  let sharedPainted: Element[] = [];
  function paintShared(elements: Element[]) {
    for (const element of sharedPainted) element.removeAttribute('data-feedback-picked');
    sharedPainted = elements;
    for (const element of sharedPainted) element.setAttribute('data-feedback-picked', '');
  }
</script>
<script lang="ts">
  import { untrack, type Snippet } from 'svelte';
  import { getFeedbackSurface } from '$lib/feedback/context';
  import type { FeedbackLocator, FeedbackTargetView } from '$lib/feedback/types';
  import { richIdentity, richTextFields, renderedField } from '$lib/feedback/annotations/rich';
  import { stripMarkdown } from '$lib/markdown';
  import { canonicalText, domTextRange, mapUniqueQuote, selectText, looseQuoteRange } from '$lib/feedback/annotations/text';
  import { nativeSelection, elementGeometry, rangeGeometry, targetView, clearNativeSelection } from '$lib/feedback/annotations/native-selection';
  import { exposeGeometry } from '$lib/feedback/annotations/expose-target';
  import { createTableRanges, type TableRangeController } from '$lib/feedback/annotations/table-range';
  let { block, index, semantic, children, anchorId, gridColumn }: {
    block: Record<string, unknown>; index: number; semantic: boolean; children: Snippet;
    anchorId?: string; gridColumn?: string;
  } = $props();
  const surface = getFeedbackSurface();
  const binding = $derived(surface?.binding);
  const identity = $derived(richIdentity(block));
  const blockId = $derived(identity?.block_id ?? '');
  let root = $state<HTMLDivElement>(), error = $state('');
  let tables = $state.raw<TableRangeController>();
  const nativeBinding = $derived.by(() => {
    const current = binding;
    return current ? { ...current, onTargetChange: (target: FeedbackTargetView | null) => {
      if (target) { tables?.clear(false); current.onTargetChange?.(target); }
      else if (!tables?.hasSelection) current.onTargetChange?.(null);
    } } : undefined;
  });
  const instance = crypto.randomUUID();
  function locator(path: string[] = [], range: Extract<FeedbackLocator, { kind: 'rich' }>['range'] = null): FeedbackLocator {
    return { kind: 'rich', block_id: blockId, identity: identity?.identity ?? 'revision_local', item_key: null, field_path: path, range };
  }
  /**
   * Map a rendered selection onto the canonical text of one authored field. Rendering (Markdown,
   * whitespace, list markers) differs from authored source, so matching uses the server-owned
   * canonical field text. When no single field matches, the comment targets the whole block.
   */
  const richTargets = $derived(!!binding?.supported_locator_kinds.includes('rich'));
  /** Whole-block target, or the whole document for plain documents rendered through rich blocks. */
  function wholeLocator(): FeedbackLocator { return richTargets ? locator([]) : { kind: 'whole' }; }
  /** Canonical text behind each mapped locator, so a multi-element pick can join two ranges exactly. */
  const mappedText = new WeakMap<object, string>();
  /** Plain documents (Markdown artifacts) anchor to the document's canonical text, not rich fields. */
  async function mapDocument(current: NonNullable<typeof binding>, exact: string, context: string) {
    let text = '', digest = '', total = Infinity;
    while (text.length < total && text.length < 1_048_576) {
      const page = await current.resolveTextScope({ kind: 'document' }, { start: text.length, limit: 32768 });
      if (page.text_page.start !== text.length) break;
      text += page.text_page.text; total = page.text_page.total_length; digest = page.descriptor.text_digest;
      if (!page.text_page.text) break;
    }
    const position = digest ? looseQuoteRange(text, exact, context) : null;
    if (!position) return null;
    const selected = selectText(text, position.start, position.end);
    const result = { locator: { kind: 'text' as const, range: { ...selected, start: position.start, end: position.end,
      representation: 'feedback-text-v1' as const, text_digest: digest } } };
    mappedText.set(result.locator, text);
    return { candidate: result, label: selected.exact };
  }
  async function map(range: Range) {
    const current = binding, id = identity;
    if (!current || !id || !root) throw new Error('This block has no authored source identity.');
    const exact = canonicalText(range.toString()).trim();
    const before = root.ownerDocument.createRange();
    before.selectNodeContents(root); before.setEnd(range.startContainer, range.startOffset);
    const context = canonicalText(before.toString()).slice(-48);
    if (!richTargets) {
      const mapped = current.supported_locator_kinds.includes('text') ? await mapDocument(current, exact, context).catch(() => null) : null;
      return mapped ?? { candidate: { locator: wholeLocator() }, label: exact.length > 160 ? `${exact.slice(0, 157)}…` : exact };
    }
    const start = renderedField(range.startContainer, root), end = renderedField(range.endContainer, root);
    const fields = richTextFields(block);
    const marked = start?.field.text && start.element === end?.element
      ? fields.filter(value => value.itemKey === null && JSON.stringify(value.path) === JSON.stringify(start.field.text)) : [];
    const firstWord = exact.split(/\s+/).find(word => word.length > 2) ?? exact;
    const candidates = (marked.length ? marked : fields.filter(value => value.text.includes(firstWord))).slice(0, 6);
    for (const field of candidates) {
      try {
        const result = await current.resolveTextScope({ kind: 'rich_field', ...id, item_key: field.itemKey, field_path: field.path },
          { start: 0, limit: 32768 });
        if (result.text_page.start !== 0) continue;
        const text = result.text_page.text;
        const position = looseQuoteRange(text, exact, context);
        if (!position) continue;
        const selected = selectText(text, position.start, position.end);
        const candidate = { locator: { kind: 'rich' as const, ...id, item_key: field.itemKey, field_path: field.path,
          range: { ...selected, start: position.start, end: position.end, representation: 'feedback-text-v1' as const,
            text_digest: result.descriptor.text_digest } } };
        mappedText.set(candidate.locator, text);
        return { candidate, label: selected.exact };
      } catch { /* Try the next field, then fall back to the whole block. */ }
    }
    return { candidate: { locator: locator([]) }, label: exact.length > 160 ? `${exact.slice(0, 157)}…` : exact };
  }
  /*
   * Comment mode is a crosshair picker, like a design-review tool. Native text selection is off
   * while it is active, so the two never compete:
   *   - click or tap picks the smallest readable element (paragraph, list item, heading, cell);
   *   - mouse drag extends the pick to a contiguous run of elements, or a rectangle of cells;
   *   - pointer outside any readable element picks nothing (never the whole page).
   * Structured rich tables keep their own cell-range controller, which publishes table ranges.
   */
  const PICKABLE = 'p,li,h1,h2,h3,h4,h5,h6,blockquote,pre,dt,dd,figcaption,img,figure,summary,td,th';
  function pick(node: Element | null): Element | null {
    if (!root || !node || node.closest('[data-feedback-rich-block]') !== root) return null;
    if (tables && node.closest('table')) return null;
    const element = node.closest(PICKABLE);
    return element && root.contains(element) && element !== root ? element : null;
  }
  /** Readable leaf elements in document order (a list item that wraps a paragraph counts once). */
  function leaves(): Element[] {
    return root ? [...root.querySelectorAll(PICKABLE)].filter(element => !element.querySelector(PICKABLE)
      && !(tables && element.closest('table'))) : [];
  }
  /** Elements covered by a pick from `anchor` to `focus`: a cell rectangle or a contiguous run. */
  function span(anchor: Element, focus: Element): Element[] {
    const anchorCell = anchor.closest('td,th'), focusCell = focus.closest('td,th');
    const table = anchorCell?.closest('table');
    if (anchorCell && focusCell && table && focusCell.closest('table') === table) {
      const position = (cell: Element) => ({ row: (cell.parentElement as HTMLTableRowElement).rowIndex, column: (cell as HTMLTableCellElement).cellIndex });
      const a = position(anchorCell), b = position(focusCell);
      const [top, bottom] = [Math.min(a.row, b.row), Math.max(a.row, b.row)];
      const [left, right] = [Math.min(a.column, b.column), Math.max(a.column, b.column)];
      return [...table.querySelectorAll('td,th')].filter(cell => {
        const at = position(cell);
        return at.row >= top && at.row <= bottom && at.column >= left && at.column <= right;
      });
    }
    const all = leaves();
    const leaf = (element: Element) => all.includes(element) ? element : all.find(item => element.contains(item)) ?? element;
    const from = all.indexOf(leaf(anchor)), to = all.indexOf(leaf(focus));
    if (from < 0 || to < 0) return [anchor];
    return all.slice(Math.min(from, to), Math.max(from, to) + 1);
  }
  function paint(elements: Element[]) {
    // Only clear when this block owns the current pick; another block's pick stays.
    if (!elements.length && !sharedPainted.some(element => root?.contains(element))) return;
    paintShared(elements);
  }
  let hovered: Element | null = null;
  function setHover(element: Element | null) {
    if (hovered === element) return;
    hovered?.removeAttribute('data-feedback-hover');
    hovered = element;
    hovered?.setAttribute('data-feedback-hover', '');
  }
  function spanGeometry(elements: Element[]) {
    const rects = elements.flatMap(element => elementGeometry(element)?.rects ?? []);
    return rects.length ? { space: 'client' as const, rects } : null;
  }
  function spanLabel(elements: Element[]) {
    const text = elements.map(element => canonicalText(element.textContent ?? '').trim()).filter(Boolean).join(elements[0]?.closest('td,th') ? ' · ' : ' ');
    return text.length > 160 ? `${text.slice(0, 157)}…` : text;
  }
  let picking = 0;
  /** Publish a pick: the covered text maps to an exact range; images and unmapped text target the block. */
  async function publish(elements: Element[]) {
    const current = binding;
    if (!root || !current || !elements.length) return;
    const request = ++picking;
    error = '';
    const label = spanLabel(elements);
    const view = (candidate: { locator: FeedbackLocator }, text: string) => targetView(current, instance, candidate, text, () => spanGeometry(elements));
    if (elements.length === 1 && elements[0].matches('img, figure')) {
      current.onTargetChange?.(view({ locator: wholeLocator() }, elements[0].getAttribute('alt') || 'Image'));
      return;
    }
    // Map one element at a time: each maps exactly, and a multi-element pick quotes its first and
    // last element so cell ranges and paragraph runs do not depend on table/layout whitespace.
    const rangeOf = (element: Element) => { const value = root!.ownerDocument.createRange(); value.selectNodeContents(element); return value; };
    try {
      const first = await map(rangeOf(elements[0]));
      const lastPick = elements.length > 1 ? await map(rangeOf(elements.at(-1)!)) : first;
      const mapped = elements.length > 1 ? join(first, lastPick) ?? first : first;
      // Bindings are recreated on every state patch; only a changed source or mode cancels the pick.
      const latest = binding;
      if (request !== picking || latest?.mode !== 'comment' || latest.source.source_token !== current.source.source_token) return;
      const exact = mapped.candidate.locator.kind === 'text' || mapped.candidate.locator.kind === 'rich' && mapped.candidate.locator.range;
      latest.onTargetChange?.(view(exact ? mapped.candidate : { locator: wholeLocator() }, exact ? mapped.label : label));
    } catch (caught) {
      if (request === picking) error = caught instanceof Error ? caught.message : 'This element cannot be commented on.';
    }
  }
  /**
   * Join two exact picks from the same canonical text (one document or one rich field) into a
   * single range covering both and everything between them.
   */
  function join(first: Awaited<ReturnType<typeof map>>, last: Awaited<ReturnType<typeof map>>) {
    const a = first.candidate.locator, b = last.candidate.locator;
    if (a.kind !== b.kind || (a.kind !== 'text' && a.kind !== 'rich')) return null;
    const aRange = a.kind === 'text' || a.kind === 'rich' ? a.range : null, bRange = b.kind === 'text' || b.kind === 'rich' ? b.range : null;
    const text = mappedText.get(a);
    if (!aRange || !bRange || !text || mappedText.get(b) !== text || aRange.text_digest !== bRange.text_digest || bRange.end <= aRange.start) return null;
    if (a.kind === 'rich' && (b.kind !== 'rich' || a.item_key !== b.item_key || JSON.stringify(a.field_path) !== JSON.stringify(b.field_path))) return null;
    const selected = selectText(text, aRange.start, bRange.end);
    const range = { ...aRange, ...selected, start: aRange.start, end: bRange.end };
    return { candidate: { locator: { ...a, range } as FeedbackLocator }, label: selected.exact };
  }
  function crosshair(node: HTMLElement, enabled: boolean | undefined) {
    let active = !!enabled;
    let drag: { id: number; anchor: Element; focus: Element } | null = null;
    let press: { x: number; y: number; at: number } | null = null;
    const at = (event: PointerEvent) => pick(node.ownerDocument.elementFromPoint(event.clientX, event.clientY));
    const interactive = (event: Event) => !!(event.target as Element)?.closest?.('a, button, input, select, textarea, summary');
    // Text selection keeps precedence: a drag over text selects text as usual (handled by the
    // native selection observer). A click without a text selection picks the element, and a
    // drag between table cells, which are not text-selectable in comment mode, picks the range.
    const selectedText = () => {
      const selection = node.ownerDocument.getSelection();
      return !!selection && !selection.isCollapsed && !!selection.toString().trim()
        && (node.contains(selection.anchorNode) || node.contains(selection.focusNode));
    };
    function down(event: PointerEvent) {
      if (!active || event.button !== 0 || interactive(event)) return;
      if (event.pointerType !== 'mouse') { press = { x: event.clientX, y: event.clientY, at: Date.now() }; return; }
      // Nested blocks all see this event; only the innermost block that owns the element acts.
      const element = at(event);
      if (!element) return;
      paintShared([]);
      const cell = element.closest('td,th');
      if (cell) { event.preventDefault(); clearNativeSelection(root); }
      drag = { id: event.pointerId, anchor: element, focus: element };
      if (cell) { paint([element]); binding?.onInteractionChange(true); }
    }
    function move(event: PointerEvent) {
      if (!active) return;
      if (!drag) { if (event.pointerType === 'mouse') setHover(at(event)); return; }
      if (event.pointerId !== drag.id) return;
      const element = at(event);
      if (!element || element === drag.focus) return;
      drag.focus = element;
      // Only cell drags preview a range; text drags show the native selection.
      if (drag.anchor.closest('td,th') && element.closest('td,th')) paint(span(drag.anchor, element));
    }
    function up(event: PointerEvent) {
      if (!active) return;
      if (drag && event.pointerId === drag.id) {
        const { anchor, focus } = drag;
        drag = null;
        binding?.onInteractionChange(false);
        if (selectedText()) { paint([]); return; }
        const cells = anchor.closest('td,th') && focus.closest('td,th');
        if (anchor !== focus && !cells) return;
        const elements = anchor === focus ? [anchor] : span(anchor, focus);
        paint(elements);
        void publish(elements);
        return;
      }
      // Touch: a short, still tap picks; any movement is a scroll and is left alone.
      const start = press; press = null;
      if (!start || event.pointerType === 'mouse' || interactive(event) || Date.now() - start.at > 600
        || Math.hypot(event.clientX - start.x, event.clientY - start.y) > 10) return;
      const element = at(event);
      if (!element) return;
      event.preventDefault();
      paint([element]);
      void publish([element]);
    }
    function cancel() { if (drag) { drag = null; paint([]); binding?.onInteractionChange(false); } press = null; }
    function leave() { setHover(null); }
    node.addEventListener('pointerdown', down); node.addEventListener('pointermove', move);
    window.addEventListener('pointerup', up); window.addEventListener('pointercancel', cancel);
    node.addEventListener('pointerleave', leave);
    function update(value: boolean | undefined) {
      active = !!value;
      node.classList.toggle('feedback-commenting', active);
      if (!active) { cancel(); setHover(null); paint([]); }
    }
    update(enabled);
    return { update, destroy() {
      setHover(null); paint([]);
      node.removeEventListener('pointerdown', down); node.removeEventListener('pointermove', move);
      window.removeEventListener('pointerup', up); window.removeEventListener('pointercancel', cancel);
      node.removeEventListener('pointerleave', leave);
    } };
  }
  function nodeFor(anchor: FeedbackLocator) {
    if (anchor.kind !== 'rich' || anchor.block_id !== blockId || !root) return null;
    if (anchor.table_range) return tables?.nodesFor(anchor)?.[0] ?? null;
    if (!anchor.field_path.length) return root;
    for (const cell of root.querySelectorAll('td')) {
      const field = renderedField(cell, root)?.field;
      if (!field) continue;
      if (JSON.stringify(field.row) === JSON.stringify(anchor.field_path)) return cell.closest('tr');
      if (JSON.stringify(field.cell) === JSON.stringify(anchor.field_path) || JSON.stringify(field.text) === JSON.stringify(anchor.field_path)) return cell;
    }
    if (anchor.range && richTextFields(block).some(field => field.itemKey === anchor.item_key && JSON.stringify(field.path) === JSON.stringify(anchor.field_path))) return root;
    return null;
  }
  /** Plain-document text anchors are located in the rendered text, ignoring whitespace differences. */
  function textRange(anchor: FeedbackLocator) {
    if (anchor.kind !== 'text' || !root) return null;
    const position = looseQuoteRange(root.textContent ?? '', anchor.range.exact, '');
    return position ? domTextRange(root, position.start, position.end) : null;
  }
  function geometry(anchor: FeedbackLocator) {
    if (anchor.kind === 'text') { const range = textRange(anchor); return range ? rangeGeometry(range) : null; }
    if (anchor.kind === 'whole' && !richTargets) return elementGeometry(root ?? null);
    if (anchor.kind === 'rich' && anchor.table_range) return tables?.geometry(anchor) ?? null;
    const node = nodeFor(anchor);
    if (anchor.kind === 'rich' && anchor.range && node) {
      const text = node.textContent ?? '', quote = anchor.range;
      // Canonical ranges contain authored Markdown; DOM offsets are in the rendered field.
      const position = mapUniqueQuote(text, quote)
        ?? looseQuoteRange(text, stripMarkdown(quote.exact), stripMarkdown(quote.prefix));
      const range = position ? domTextRange(node as HTMLElement, position.start, position.end) : null;
      return range ? rangeGeometry(range) : null;
    }
    return elementGeometry(node);
  }
  $effect(() => {
    if (!root) return;
    if (!['table', 'comparison_matrix', 'decision_matrix'].includes(String(block.type ?? block.kind))) return;
    const controller = createTableRanges(root, instance, message => { error = message; });
    tables = controller;
    controller.update(untrack(() => binding ?? undefined));
    return () => { controller.destroy(); tables = undefined; };
  });
  $effect(() => { tables?.update(binding ?? undefined); });
  // Text selection needs no mode: the first selection loads comments for this source, then the
  // existing native selection observer maps it once the binding exists.
  let awaitingBinding = false;
  $effect(() => {
    const node = root, current = surface;
    if (binding || !node || !current?.prepare) return;
    function intent() {
      const selection = node!.ownerDocument.getSelection();
      if (awaitingBinding || !selection || selection.isCollapsed || !node!.contains(selection.anchorNode)
        || !node!.contains(selection.focusNode) || !selection.toString().trim()) return;
      awaitingBinding = true;
      void Promise.resolve(current!.prepare?.()).catch(() => { awaitingBinding = false; });
    }
    node.addEventListener('pointerup', intent); node.addEventListener('keyup', intent);
    return () => { node.removeEventListener('pointerup', intent); node.removeEventListener('keyup', intent); };
  });
  $effect(() => {
    if (!binding || !root || !awaitingBinding) return;
    awaitingBinding = false;
    const node = root;
    queueMicrotask(() => node.dispatchEvent(new Event('pointerup')));
  });
  $effect(() => {
    if (!binding) return;
    return binding.registerAdapter(instance, {
      clearSelection: () => { tables?.clear(); clearNativeSelection(root); paint([]); }, targetGeometry: geometry,
      exposeTarget: (anchor, visible) => exposeGeometry(root, () => geometry(anchor), visible),
      locate: anchor => { if (anchor.kind === 'text' || anchor.kind === 'whole') { const found = anchor.kind === 'text' ? textRange(anchor)?.startContainer.parentElement : root; if (!found) return { status: 'not_located' }; found.scrollIntoView({ block: 'center' }); return { status: 'exact' }; } const node = nodeFor(anchor); if (!node || anchor.kind === 'rich' && (anchor.range || anchor.table_range) && !geometry(anchor)) return { status: 'not_located' }; node.scrollIntoView({ block: 'center' }); return { status: 'exact' }; },
    });
  });
</script>
<div bind:this={root} id={anchorId} style:grid-column={gridColumn} tabindex="-1"
  class="rich-block-anchor relative" data-feedback-rich-block={blockId}
  use:nativeSelection={{ binding: nativeBinding, instance, map, error: message => { error = message; } }}
   use:crosshair={binding?.mode === 'comment'} aria-label="Document block">
  {@render children()}
  {#if error}<p role="alert" class="text-xs text-danger">{error}</p>{/if}
</div>
<style>
  .rich-block-anchor { flex-direction: column; }
  /* Comment mode is a crosshair picker; native text selection is off so it never competes. */
  /* Text stays selectable (text selection wins); cells are areas, so they pick instead. */
  .rich-block-anchor:global(.feedback-commenting), .rich-block-anchor:global(.feedback-commenting) :global(*) { cursor: crosshair; }
  .rich-block-anchor:global(.feedback-commenting) :global(:is(td, th)) { user-select: none; -webkit-user-select: none; }
  .rich-block-anchor:global(.feedback-commenting) :global(:is(a, button, input, select, textarea, summary)) { cursor: pointer; }
  .rich-block-anchor :global([data-feedback-hover]) { outline: 1px dashed color-mix(in srgb, var(--rich-accent) 70%, transparent); outline-offset: 3px; border-radius: 3px; }
  .rich-block-anchor :global([data-feedback-picked]) { background-color: color-mix(in srgb, var(--rich-accent) 16%, transparent); border-radius: 3px; }
  /* Cells only tint; the shared target paint draws one outline around the whole range. */
  .rich-block-anchor :global([data-feedback-table-selected]) { background-color: color-mix(in srgb, var(--rich-accent) 12%, transparent); }
  :global(.rich-grid > .rich-block-list) > .rich-block-anchor > :global(:is(.rich-card, .rich-metric, .rich-dashboard-card)) { flex: 1; height: auto; }
  :global(.rich-grid > .rich-block-list) > .rich-block-anchor > :global(.rich-card:not(.has-media)) { min-height: auto; }
</style>
