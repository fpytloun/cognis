<script lang="ts">
  import ChevronDown from 'lucide-svelte/icons/chevron-down';
  import ChevronUp from 'lucide-svelte/icons/chevron-up';
  import FileCode2 from 'lucide-svelte/icons/file-code-2';
  import { onMount, onDestroy, tick, untrack } from 'svelte';
  import type { FeedbackAnnotationAdapter, FeedbackHighlight, FeedbackLocator, FeedbackSurfaceBinding } from '$lib/feedback/types';
  import { annotationDiffRows, diffHunkText, selectDiffLines } from '$lib/feedback/annotations/diff';
  import { textDigest } from '$lib/feedback/annotations/digest';
  import { surfaceIdentity } from '$lib/feedback/annotations/surface-identity';
  import { elementGeometry, targetView } from '$lib/feedback/annotations/native-selection';
  import { exposeGeometry } from '$lib/feedback/annotations/expose-target';

  import { parseFileDiff, type FileDiff, type ParsedDiffLine } from '$lib/diff';
  import { loadHighlighter } from '$lib/syntax/lazy-highlight';

  // Loaded on demand so highlight.js stays out of the chat startup bundle.
  let hljs = $state<Awaited<ReturnType<typeof loadHighlighter>> | null>(null);
  onMount(() => {
    void loadHighlighter().then((loaded) => { hljs = loaded; });
  });

  let {
    diffs,
    collapsible = true,
    collapsedByDefault = false,
    onExpand,
    annotationBindings = [],
  } = $props<{
    diffs: FileDiff[];
    collapsible?: boolean;
    collapsedByDefault?: boolean;
    onExpand?: (() => void) | undefined;
    annotationBindings?: (FeedbackSurfaceBinding | null)[];
  }>();

  let collapsed = $state<Record<string, boolean>>({});
  type Selection = { file: number; side: 'old' | 'new'; start: number; end: number };
  let selection = $state<Selection | null>(null);
  let annotationError = $state('');
  let root = $state<HTMLDivElement>();
  const instance = crypto.randomUUID();
  let selectionGeneration = 0;
  let selectionBinding: FeedbackSurfaceBinding | null = null;
  /** Sole owner of line, hunk, clear and evidence-replacement transitions. */
  function replaceSelection(next: Selection | null) {
    if (!next) dragging = null;
    selectionGeneration++;
    const previousBinding = selectionBinding;
    selection = next;
    selectionBinding = next ? annotationBindings[next.file] ?? null : null;
    annotationError = '';
    if (previousBinding && surfaceIdentity(previousBinding) !== surfaceIdentity(selectionBinding)) previousBinding.onInteractionChange(false);
    selectionBinding?.onInteractionChange(true);
    previousBinding?.onTargetChange?.(null);
    if (next) void comment(next.file);
    else previousBinding?.onTargetChange?.(null);
  }
  function clearSelection() { replaceSelection(null); }
  onDestroy(clearSelection);
  function lineSelect(file: number, side: 'old' | 'new', number: number, extend: boolean) {
    if (!evidenceFor(file)?.eligible) return;
    replaceSelection(extend && selection?.file === file && selection.side === side
      ? { ...selection, end: number } : { file, side, start: number, end: number });
  }
  function lineKey(event: KeyboardEvent, file: number, side: 'old' | 'new', number: number) {
    if (!['ArrowUp', 'ArrowDown'].includes(event.key)) return;
    event.preventDefault();
    const numbers = [...new Set<number>(annotationRows[file].flatMap((row: AnnotationRow) => {
      const value = side === 'old' ? row.oldLine : row.newLine; return value === null ? [] : [value];
    }))];
    const next = numbers[Math.max(0, Math.min(numbers.length - 1, numbers.indexOf(number) + (event.key === 'ArrowDown' ? 1 : -1)))];
    if (next === undefined) return;
    lineSelect(file, side, next, event.shiftKey);
    root?.querySelector<HTMLElement>(`[data-feedback-file="${file}"] [data-line-control="${side}-${next}"]`)?.focus({ preventScroll: true });
  }
  let dragging: number | null = null;
  function beginRange(event: PointerEvent) {
    event.preventDefault();
    dragging = event.pointerId;
    root?.setPointerCapture(event.pointerId);
  }
  function dragRange(event: PointerEvent) {
    const range = selection;
    if (!range || dragging !== event.pointerId) return;
    event.preventDefault();
    const cells = Array.from(root?.querySelectorAll<HTMLElement>(`[data-feedback-file="${range.file}"] [data-feedback-${range.side}]`) ?? []);
    const nearest = cells.filter(cell => cell.getAttribute(`data-feedback-${range.side}`))
      .sort((a, b) => Math.abs(a.getBoundingClientRect().top + a.offsetHeight / 2 - event.clientY)
        - Math.abs(b.getBoundingClientRect().top + b.offsetHeight / 2 - event.clientY))[0];
    if (nearest) lineSelect(range.file, range.side, Number(nearest.getAttribute(`data-feedback-${range.side}`)), true);
  }
  function evidenceFor(file: number) {
    const diff = diffs[file], binding = annotationBindings[file];
    return diff ? { source: surfaceIdentity(binding), path: diff.path, text: diff.diff,
      linesAvailable: !!annotationRows[file]?.length,
      // Line selection is always available; there is no separate comment mode for diffs.
      eligible: !!binding?.source.capabilities.comment && binding.mode !== 'area' && binding.mode !== 'draw' } : null;
  }
  function lineComments(file: number, line: { oldLine: number | null; newLine: number | null }) {
    const binding = annotationBindings[file];
    if (binding?.source.source.kind !== 'work_diff') return [];
    return binding.highlights.filter(({ locator }: FeedbackHighlight) => locator.kind === 'diff' && locator.scope === 'lines'
      && (locator.side === 'new' ? line.newLine : line.oldLine) === locator.start_line);
  }
  type Evidence = ReturnType<typeof evidenceFor>;
  function sameEvidence(left: Evidence, right: Evidence): boolean {
    return !!left && !!right && left.source === right.source && left.path === right.path && left.text === right.text
      && left.linesAvailable === right.linesAvailable && left.eligible === right.eligible;
  }
  let previousEvidence: Evidence[] = [];
  $effect(() => {
    const next = diffs.map((_diff: FileDiff, index: number) => evidenceFor(index));
    if (next.length !== previousEvidence.length || next.some((proof: Evidence, index: number) => !sameEvidence(proof, previousEvidence[index] ?? null))) untrack(clearSelection);
    previousEvidence = next;
  });
  async function comment(file: number, whole = false) {
    const binding = annotationBindings[file], range = selection, generation = selectionGeneration, evidence = evidenceFor(file);
    if (!binding || !evidence?.eligible || !whole && range?.file !== file) return;
    try {
      if (whole) binding.onAnchorCandidate({ locator: { kind: 'diff', scope: 'file', side: null, start_line: null, end_line: null, hunk_digest: null, quote: null } });
      else if (range?.file === file) {
        const selected = selectDiffLines(annotationDiffRows(diffs[file]), range.side, range.start, range.end);
        const digest = await textDigest(diffHunkText(evidence.text, selected.hunk));
        if (!sameEvidence(evidenceFor(file), evidence) || generation !== selectionGeneration) return;
        const locator = { kind: 'diff' as const, scope: 'lines' as const, side: range.side, start_line: selected.start_line, end_line: selected.end_line, quote: selected.quote, hunk_digest: digest };
        binding.onTargetChange?.({
          ...targetView(binding, `${instance}-${file}`, { locator }, `${range.side} lines ${selected.start_line}–${selected.end_line}`, () => diffTargetGeometry(file, locator)),
          controlGeometry: () => diffControlGeometry(file, locator),
        });
      }
      if (whole && generation === selectionGeneration) clearSelection();
    } catch (cause) { if (generation === selectionGeneration) annotationError = (cause as Error).message; }
  }
  function diffGeometry(index: number, anchor: Parameters<FeedbackAnnotationAdapter['locate']>[0]) {
    if (anchor.kind !== 'diff') return null;
    if (anchor.scope === 'file') return elementGeometry(root?.querySelector(`[data-feedback-file="${index}"]`));
    const nodes = Array.from(root?.querySelectorAll<HTMLElement>(`[data-feedback-file="${index}"] [data-feedback-${anchor.side}]`) ?? [])
      .filter(node => { const line = Number(node.getAttribute(`data-feedback-${anchor.side}`)); return line >= anchor.start_line! && line <= anchor.end_line!; });
    return { space: 'client' as const, rects: nodes.flatMap(node => elementGeometry(node.closest('tr'))?.rects ?? []) };
  }
  function diffControlGeometry(index: number, anchor: Parameters<FeedbackAnnotationAdapter['locate']>[0]) {
    if (anchor.kind !== 'diff' || anchor.scope !== 'lines') return null;
    const cell = root?.querySelector<HTMLElement>(`[data-feedback-file="${index}"] [data-feedback-${anchor.side}="${anchor.start_line}"]`);
    const row = cell?.closest('tr'), viewport = row?.closest('table')?.parentElement;
    if (!row || !viewport) return null;
    const first = row.cells[0].getBoundingClientRect(), last = row.cells[1].getBoundingClientRect();
    const clip = viewport.getBoundingClientRect();
    if (!first.height || first.left < clip.left || last.right > clip.right || first.top < clip.top || first.bottom > clip.bottom) return null;
    return { space: 'client' as const, rects: [{ left: first.left + 1, top: first.top,
      width: Math.max(0, last.right - first.left - 2), height: first.height }] };
  }
  function diffTargetGeometry(index: number, anchor: Parameters<FeedbackAnnotationAdapter['locate']>[0]) {
    const geometry = diffGeometry(index, anchor);
    if (!geometry || anchor.kind !== 'diff' || anchor.scope !== 'lines') return geometry;
    const cell = root?.querySelector(`[data-feedback-file="${index}"] [data-feedback-${anchor.side}="${anchor.start_line}"]`);
    const clip = cell?.closest('table')?.parentElement?.getBoundingClientRect();
    if (!clip?.width || !clip.height) return geometry;
    return { ...geometry, rects: geometry.rects.map(rect => {
      const left = Math.max(rect.left, clip.left), top = Math.max(rect.top, clip.top);
      return { left, top, width: Math.max(0, Math.min(rect.left + rect.width, clip.right) - left),
        height: Math.max(0, Math.min(rect.top + rect.height, clip.bottom) - top) };
    }).filter(rect => rect.width > 0 && rect.height > 0) };
  }
  $effect(() => {
    const releases = annotationBindings.map((binding: FeedbackSurfaceBinding | null, index: number) => binding?.registerAdapter(`${instance}-${index}`, {
      clearSelection,
      targetGeometry: anchor => diffTargetGeometry(index, anchor),
      editorHost: (anchor?: FeedbackLocator | null) => {
        if (!anchor || anchor.kind === 'whole' || (anchor.kind === 'diff' && anchor.scope === 'file')) {
          return root?.querySelector<HTMLElement>(`[data-feedback-editor-host="${index}"]:not([data-feedback-editor-row])`) ?? null;
        }
        const source = binding?.source.source;
        if (anchor.kind !== 'diff' || anchor.scope !== 'lines' || source?.kind !== 'work_diff') return null;
        const row = parsedDiffs[index]?.lines.findIndex((line: ParsedDiffLine) =>
          (anchor.side === 'old' ? line.oldLine : line.newLine) === anchor.end_line);
        return row === undefined || row < 0 ? null
          : root?.querySelector<HTMLElement>(`[data-feedback-editor-host="${index}"][data-feedback-editor-row="${row}"]`) ?? null;
      },
      exposeTarget: (anchor, visible) => exposeGeometry(root, () => diffGeometry(index, anchor), visible),
      locate: async (anchor: Parameters<FeedbackAnnotationAdapter['locate']>[0]) => {
        if (anchor.kind !== 'diff') return { status: 'not_located' };
        if (anchor.scope === 'lines') {
          try {
            const selected = selectDiffLines(annotationDiffRows(diffs[index]), anchor.side!, anchor.start_line!, anchor.end_line!);
            if (selected.quote !== anchor.quote || await textDigest(diffHunkText(diffs[index].diff, selected.hunk)) !== anchor.hunk_digest) return { status: 'not_located' };
          } catch { return { status: 'not_located' }; }
        }
        collapsed = { ...collapsed, [keyFor(diffs[index].path, index)]: false };
        await tick();
        const node = anchor.scope === 'file' ? root?.querySelector(`[data-feedback-file="${index}"]`)
          : root?.querySelector(`[data-feedback-file="${index}"] [data-feedback-${anchor.side}="${anchor.start_line}"]`);
        node?.scrollIntoView({ block: 'center' });
        return { status: node ? 'exact' : 'not_located' };
      },
    } satisfies FeedbackAnnotationAdapter));
    return () => { releases.forEach((release: (() => void) | undefined) => release?.()); };
  });

  const parsedDiffs = $derived(diffs.map(parseFileDiff));
  const annotationRows = $derived(diffs.map(annotationDiffRows));
  type AnnotationRow = ReturnType<typeof annotationDiffRows>[number];

  function keyFor(path: string, index: number): string {
    return `${path || 'omitted'}:${index}`;
  }

  function toggle(path: string, index: number): void {
    const key = keyFor(path, index);
    collapsed = { ...collapsed, [key]: !(collapsed[key] ?? collapsedByDefault) };
  }

  function escapeHtml(value: string): string {
    return value
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
  }

  function highlighted(line: ParsedDiffLine, language: string | null): string {
    if (line.type === 'hunk' || line.type === 'file' || line.type === 'meta') {
      return escapeHtml(line.content);
    }
    if (!line.content) return '';
    if (!hljs) return escapeHtml(line.content);
    try {
      if (language && hljs.getLanguage(language)) {
        return hljs.highlight(line.content, { language, ignoreIllegals: true }).value;
      }
      return hljs.highlightAuto(line.content).value;
    } catch {
      return escapeHtml(line.content);
    }
  }

  function rowClass(type: string): string {
    if (type === 'add') return 'bg-success-subtle text-foreground';
    if (type === 'remove') return 'bg-danger-subtle text-foreground';
    if (type === 'hunk') return 'bg-info-subtle text-info';
    if (type === 'file' || type === 'meta') return 'bg-raised text-muted';
    return 'text-secondary';
  }

  function marker(type: string): string {
    if (type === 'add') return '+';
    if (type === 'remove') return '-';
    return ' ';
  }
  function selectedLine(file: number, line: ParsedDiffLine): boolean {
    if (!selection || selection.file !== file) return false;
    const number = selection.side === 'old' ? line.oldLine : line.newLine;
    return number !== null && number >= Math.min(selection.start, selection.end) && number <= Math.max(selection.start, selection.end);
  }
  function textSelection(node: HTMLElement) {
    function code(value: Node): HTMLElement | null {
      const element = value instanceof Element ? value : value.parentElement;
      const cell = element?.closest<HTMLElement>('[data-diff-code]');
      return cell && node.contains(cell) ? cell : null;
    }
    function read() {
      const native = window.getSelection();
      if (!native || native.isCollapsed || native.rangeCount !== 1 || !native.toString().trim()) return;
      const range = native.getRangeAt(0), start = code(range.startContainer), end = code(range.endContainer);
      if (!start || !end) return;
      const file = Number(start.dataset.diffCode);
      if (end.dataset.diffCode !== start.dataset.diffCode || !evidenceFor(file)?.eligible) return;
      const side = !start.dataset.newLine || !end.dataset.newLine ? 'old' : 'new';
      const first = Number(side === 'old' ? start.dataset.oldLine : start.dataset.newLine);
      const last = Number(side === 'old' ? end.dataset.oldLine : end.dataset.newLine);
      if (!first || !last) return;
      replaceSelection({ file, side, start: first, end: last });
    }
    function click(event: MouseEvent) {
      if (window.getSelection()?.toString().trim() || !(event.target instanceof Node)) return;
      const cell = code(event.target);
      if (!cell) return;
      const file = Number(cell.dataset.diffCode), side = cell.dataset.newLine ? 'new' : 'old';
      const number = Number(side === 'new' ? cell.dataset.newLine : cell.dataset.oldLine);
      if (number) lineSelect(file, side, number, event.shiftKey);
    }
    node.addEventListener('pointerup', read); node.addEventListener('keyup', read); node.addEventListener('click', click);
    return { destroy() { node.removeEventListener('pointerup', read); node.removeEventListener('keyup', read); node.removeEventListener('click', click); } };
  }
</script>

<svelte:window onpointermove={dragRange} onpointerup={() => dragging = null} onpointercancel={() => dragging = null} />
<div bind:this={root} use:textSelection class="relative space-y-3">
  {#if annotationError}<p role="alert" class="absolute right-0 top-0 z-10 max-w-[60%] rounded bg-surface p-2 text-xs text-danger">{annotationError}</p>{/if}
  {#each parsedDiffs as diff, index (keyFor(diff.path, index))}
    {@const key = keyFor(diff.path, index)}
    {@const isCollapsed = collapsed[key] ?? collapsedByDefault}
    {@const hasContent = Boolean(diffs[index]?.diff?.trim())}
    {@const contentId = `file-diff-content-${index}-${key.replace(/[^a-zA-Z0-9_-]/g, '-')}`}
    {@const gutterWidth = `calc(${Math.max(2, String(diff.lines.reduce((max: number, line: ParsedDiffLine) => Math.max(max, line.oldLine ?? 0, line.newLine ?? 0), 0)).length)}ch + 1rem)`}
    {#if diff.omittedCount > 0 && !diff.path}
      <div class="rounded-xl border border-warning-border bg-warning-subtle px-3 py-2 text-xs text-warning">
        {diff.omittedCount} additional file diff{diff.omittedCount === 1 ? '' : 's'} omitted from this preview.
      </div>
    {:else}
      <section data-feedback-file={index} class="overflow-hidden rounded-xl border border-control-border bg-surface shadow-inner" style:--diff-gutter-width={gutterWidth}>
        <div class="flex items-stretch border-b border-control-border bg-inset">
        <button
          class="flex min-w-0 flex-1 items-center gap-3 px-3 py-2 text-left transition hover:bg-surface-hover"
          type="button"
          onclick={() => hasContent && collapsible ? toggle(diff.path, index) : onExpand?.()}
          aria-expanded={hasContent && collapsible ? !isCollapsed : undefined}
          aria-controls={hasContent && collapsible ? contentId : undefined}
        >
          <span class="inline-flex h-8 w-8 shrink-0 items-center justify-center rounded-lg border border-control-border bg-surface text-[10px] font-semibold text-cyan-tone" title={diff.languageLabel}>
            {#if diff.iconLabel}
              {diff.iconLabel}
            {:else}
              <FileCode2 class="h-4 w-4" />
            {/if}
          </span>
          <span class="min-w-0 flex-1">
            <span class="block truncate font-mono text-xs font-medium text-foreground" title={diff.path}>{diff.path.split('/').at(-1) || diff.path}</span>
            <span class="mt-0.5 block truncate text-[11px] text-quiet" title={diff.path}>{diff.path.includes('/') ? diff.path.slice(0, diff.path.lastIndexOf('/')) : diff.languageLabel}</span>
          </span>
          <span class="flex shrink-0 items-center gap-2 font-mono text-xs">
            {#if diff.additions > 0}<span class="text-success">+{diff.additions}</span>{/if}
            {#if diff.deletions > 0}<span class="text-danger">-{diff.deletions}</span>{/if}
            {#if hasContent && collapsible}
              {#if isCollapsed}<ChevronDown class="h-4 w-4 text-quiet" />{:else}<ChevronUp class="h-4 w-4 text-quiet" />{/if}
            {:else if onExpand}
              <span class="hidden rounded border border-control-border px-2 py-1 text-[10px] text-accent sm:inline-flex">{hasContent ? 'Expand diff' : 'View diff'}</span>
            {/if}
          </span>
        </button>
        {#if annotationBindings[index]?.source.source.kind === 'work_diff' && annotationBindings[index]?.highlights.length}
          <button type="button" class="feedback-icon my-auto shrink-0 text-accent" aria-label="View file comments" title="View file comments"
            onclick={() => annotationBindings[index]?.onSelectThread(annotationBindings[index]!.highlights[0].thread_id)}>
            <svg class="h-3.5 w-3.5" viewBox="0 0 16 16" fill="none" stroke="currentColor" aria-hidden="true"><path d="M2 2h12v9H7l-5 3z" stroke-linejoin="round" /></svg>
            <span class="text-[10px]">{annotationBindings[index]!.highlights.length}</span>
          </button>
        {/if}
        </div>

        <div data-feedback-editor-host={index} class="empty:hidden border-t border-separator p-2"></div>
        {#if hasContent && (!collapsible || !isCollapsed)}
          <div class="diff-scroll max-h-[58vh] overflow-auto text-xs leading-5" id={contentId}>
            <table class="w-full border-collapse font-mono">
              <tbody>
                {#each diff.lines as line, lineIndex (`${key}:${lineIndex}`)}
                  {@const comments = lineComments(index, line)}
                  <tr class={rowClass(line.type)} class:selected-diff-line={selectedLine(index, line)}>
                    <td data-feedback-old={line.oldLine} class="diff-gutter gutter-old relative select-none border-r border-separator bg-canvas px-2 text-right text-quiet">
                      {#if line.oldLine !== null && evidenceFor(index)?.eligible && annotationRows[index].length}
                        <button type="button" class="line-number" data-line-control={`old-${line.oldLine}`} aria-label={`Select old line ${line.oldLine}`} onkeydown={event => lineKey(event, index, 'old', line.oldLine!)} onclick={event => lineSelect(index, 'old', line.oldLine!, event.shiftKey)}>{line.oldLine}</button>
                      {:else}{line.oldLine ?? ''}{/if}
                      {#if selection?.file === index && selection.side === 'old' && selection.end === line.oldLine && evidenceFor(index)?.eligible}
                        <button type="button" class="range-handle" aria-label="Adjust selected line range" title="Drag to extend; Shift+Arrow extends with the keyboard" onkeydown={event => lineKey(event, index, 'old', line.oldLine!)}
                          onpointerdown={beginRange}><span></span></button>
                      {/if}
                    </td>
                    <td data-feedback-new={line.newLine} class="diff-gutter gutter-new relative select-none border-r border-separator bg-canvas px-2 text-right text-quiet">
                      {#if line.newLine !== null && evidenceFor(index)?.eligible && annotationRows[index].length}
                        <button type="button" class="line-number" data-line-control={`new-${line.newLine}`} aria-label={`Select new line ${line.newLine}`} onkeydown={event => lineKey(event, index, 'new', line.newLine!)} onclick={event => lineSelect(index, 'new', line.newLine!, event.shiftKey)}>{line.newLine}</button>
                      {:else}{line.newLine ?? ''}{/if}
                      {#if selection?.file === index && selection.side === 'new' && selection.end === line.newLine && evidenceFor(index)?.eligible}
                        <button type="button" class="range-handle" aria-label="Adjust selected line range" title="Drag to extend; Shift+Arrow extends with the keyboard" onkeydown={event => lineKey(event, index, 'new', line.newLine!)}
                          onpointerdown={beginRange}><span></span></button>
                      {/if}
                    </td>
                    <td class="diff-marker select-none bg-canvas px-2 text-center text-quiet">
                      {#if comments.length}
                        <button type="button" class="inline-flex h-5 min-w-5 items-center justify-center rounded text-accent hover:bg-accent-subtle" aria-label={`${comments.length} comment${comments.length === 1 ? '' : 's'} on this line`} title="View line comments"
                          onclick={() => annotationBindings[index]?.onSelectThread(comments[0].thread_id)}>
                          <svg class="h-3.5 w-3.5" viewBox="0 0 16 16" fill="none" stroke="currentColor" aria-hidden="true"><path d="M2 2h12v9H7l-5 3z" stroke-linejoin="round" /></svg>
                        </button>
                      {:else}{marker(line.type)}{/if}
                    </td>
                    <td data-diff-code={index} data-old-line={line.oldLine ?? undefined} data-new-line={line.newLine ?? undefined} class="min-w-[28rem] whitespace-pre px-2 [tab-size:2]">
                      {@html highlighted(line, diff.language)}
                    </td>
                  </tr>
                  {#if line.oldLine !== null || line.newLine !== null}
                    <tr>
                      <td colspan="4" class="p-0">
                        <div data-feedback-editor-host={index} data-feedback-editor-row={lineIndex}
                          class="diff-inline-editor p-2 font-sans text-sm empty:hidden"></div>
                      </td>
                    </tr>
                  {/if}
                {/each}
              </tbody>
            </table>
          </div>
        {/if}
      </section>
    {/if}
  {/each}
</div>
<style>
  .diff-scroll { container-type: inline-size; }
  .diff-inline-editor { position: sticky; left: 0; width: 100cqw; }
  .diff-gutter { position: sticky; z-index: 1; width: var(--diff-gutter-width); min-width: var(--diff-gutter-width); max-width: var(--diff-gutter-width); }
  .gutter-old { left: 0; }
  .gutter-new { left: var(--diff-gutter-width); }
  .diff-marker { position: sticky; z-index: 1; left: calc(2 * var(--diff-gutter-width)); }
  .selected-diff-line > td { box-shadow: inset 0 1px rgb(var(--color-accent) / .5), inset 0 -1px rgb(var(--color-accent) / .5); }
  .selected-diff-line > td:not(.diff-gutter):not(.diff-marker) { background: rgb(var(--color-accent) / .12); }
  .line-number { display: inline; min-height: 0; min-width: 0; padding: 0; font: inherit; line-height: inherit; vertical-align: baseline; }
  .range-handle { position: absolute; z-index: 2; left: -4px; top: -12px; width: 44px; height: 44px; display: grid; place-items: center; touch-action: none; }
  .range-handle span { width: 16px; height: 16px; border: 2px solid var(--color-accent); border-radius: 50%; background: var(--color-surface); }
</style>
