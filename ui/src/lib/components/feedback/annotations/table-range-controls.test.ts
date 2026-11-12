import { fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import TableRangeHarness from './TableRangeHarness.svelte';
import type { FeedbackAnnotationAdapter, FeedbackLocator, FeedbackSurfaceBinding, FeedbackTargetView } from '$lib/feedback/types';

function binding(): FeedbackSurfaceBinding {
  return { source: { subject_id: 'table-subject', source_token: 'table-source-token',
    source: { kind: 'document', content_ref: 'table-document-ref' }, content_digest: 'a'.repeat(64),
    availability: 'available', permitted_locator_kinds: ['rich'], context: { kind: 'standalone' },
    lineage_id: null, revision: 1, latest_content_ref: null,
    capabilities: { read: true, comment: true, moderate: false, dispatch: false, disclose: false },
    title: 'Release data', format: 'rich', canonical_text: null },
    context: { kind: 'standalone' }, mode: 'comment', tool: 'text', supported_locator_kinds: ['rich'],
    completeness: 'available', highlights: [], selected_thread_id: null,
    resolveTextScope: vi.fn(), onTargetChange: vi.fn(), onAnchorCandidate: vi.fn(),
    onSelectThread: vi.fn(), onInteractionChange: vi.fn(), registerAdapter: vi.fn(() => () => {}) };
}
function target(value: FeedbackSurfaceBinding): FeedbackTargetView {
  return vi.mocked(value.onTargetChange!).mock.calls.map(([target]) => target).filter(value => value !== null).at(-1)!;
}
function adapter(value: FeedbackSurfaceBinding): FeedbackAnnotationAdapter {
  return vi.mocked(value.registerAdapter).mock.calls.at(-1)![1];
}
function pointer(type: string, x = 150, y = 150, pointerType = 'mouse') {
  const event = new MouseEvent(type, { bubbles: true, cancelable: true, clientX: x, clientY: y, button: 0 });
  Object.defineProperties(event, { pointerId: { value: 7 }, pointerType: { value: pointerType } });
  return event;
}
const originalScroll = Object.getOwnPropertyDescriptor(HTMLElement.prototype, 'scrollIntoView');
const originalElementFromPoint = Object.getOwnPropertyDescriptor(document, 'elementFromPoint');
beforeEach(() => {
  Object.defineProperty(HTMLElement.prototype, 'scrollIntoView', { configurable: true, value: vi.fn() });
  vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockImplementation(function (this: HTMLElement) {
    const cell = this as HTMLTableCellElement, tr = cell.closest('tr') as HTMLTableRowElement | null;
    const row = tr?.parentElement?.tagName === 'TBODY' ? [...tr.parentElement.children].indexOf(tr) + 1 : 0;
    return new DOMRect(100 + (cell.cellIndex >= 0 ? cell.cellIndex * 100 : 0), 100 + row * 40, 100, 40);
  });
  // JSDOM has no hit testing; use the same cell geometry as the pointer fixture.
  Object.defineProperty(document, 'elementFromPoint', { configurable: true, value: (x: number, y: number) =>
    [...document.querySelectorAll('td,th')].find(cell => {
      const rect = cell.getBoundingClientRect();
      return x >= rect.left && x < rect.right && y >= rect.top && y < rect.bottom;
    }) ?? null });
});
afterEach(() => {
  vi.restoreAllMocks(); window.getSelection()?.removeAllRanges();
  if (originalScroll) Object.defineProperty(HTMLElement.prototype, 'scrollIntoView', originalScroll);
  else Reflect.deleteProperty(HTMLElement.prototype, 'scrollIntoView');
  if (originalElementFromPoint) Object.defineProperty(document, 'elementFromPoint', originalElementFromPoint);
  else Reflect.deleteProperty(document, 'elementFromPoint');
});

async function drag(cells: Element[], points: [number, number][]) {
  const [first, ...rest] = points;
  await fireEvent(cells[0], pointer('pointerdown', ...first));
  for (const [index, point] of rest.entries()) await fireEvent(cells[Math.min(index + 1, cells.length - 1)], pointer('pointermove', ...point));
  await fireEvent(cells.at(-1)!, pointer('pointerup', ...points.at(-1)!));
}
const cells = (container: HTMLElement) => [...container.querySelectorAll('tbody td')];

describe('crosshair cell selection in comment mode', () => {
  it('selects a cell range by dragging, with one rect per selected cell and no handles', async () => {
    const value = binding(), view = render(TableRangeHarness, { binding: value });
    await waitFor(() => expect(cells(view.container).length).toBeGreaterThan(0));
    expect(screen.queryByRole('button', { name: /^Select / })).toBeNull();
    const all = cells(view.container);
    await drag([all[0], all[3]], [[150, 160], [250, 200]]);
    expect(target(value).candidate.locator).toMatchObject({ table_range: { scope: 'cells', rows: [0, 1], column_start: 0, column_end: 2 } });
    expect(target(value).geometry()?.rects).toHaveLength(4);
    expect(view.container.querySelectorAll('[data-feedback-table-selected]')).toHaveLength(4);
  });
  it('leaves tables untouched outside comment mode and for touch input', async () => {
    const viewing = { ...binding(), mode: 'view' as const }, view = render(TableRangeHarness, { binding: viewing });
    await waitFor(() => expect(cells(view.container).length).toBeGreaterThan(0));
    const down = pointer('pointerdown', 150, 160);
    await fireEvent(cells(view.container)[0], down);
    expect(down.defaultPrevented).toBe(false);
    view.unmount();
    const value = binding(), touch = render(TableRangeHarness, { binding: value });
    await waitFor(() => expect(cells(touch.container).length).toBeGreaterThan(0));
    const swipe = pointer('pointerdown', 150, 160, 'touch');
    await fireEvent(cells(touch.container)[0], swipe);
    await fireEvent(cells(touch.container)[0], pointer('pointerup', 150, 190, 'touch'));
    expect(swipe.defaultPrevented).toBe(false);
    for (const candidate of [viewing, value])
      expect(vi.mocked(candidate.onTargetChange!).mock.calls.some(([target]) => target?.candidate.locator.kind === 'rich' && target.candidate.locator.table_range)).toBe(false);
  });
  it('keeps exact authored memberships after sorting and reconstructs saved highlights', async () => {
    const value = binding(), view = render(TableRangeHarness, { binding: value });
    await fireEvent.click(await screen.findByRole('button', { name: 'Sort by Name' }));
    await waitFor(() => expect(view.container.querySelector('tbody td')?.textContent).toBe('Alpha'));
    const all = cells(view.container);
    await drag([all[0], all[3]], [[150, 160], [250, 200]]);
    const locator = target(value).candidate.locator;
    expect(locator).toMatchObject({ table_range: { rows: [1, 2] } });
    view.unmount();
    const next: FeedbackSurfaceBinding = { ...binding(), highlights: [
      { thread_id: 'saved-table-thread', anchor_id: 'saved-table-anchor', category: 'exact', locator, label: 'Saved cells' },
    ] };
    const restored = render(TableRangeHarness, { binding: next });
    await waitFor(() => expect(restored.container.querySelectorAll('[data-feedback-table-selected]')).toHaveLength(4));
    expect(adapter(next).targetGeometry?.(locator)?.rects).toHaveLength(4);
    expect(adapter(next).locate(locator)).toEqual({ status: 'exact' });
  });
  it.each(['source', 'context', 'mode', 'capability'] as const)('clears the selection after %s changes', async change => {
    const value = binding(), view = render(TableRangeHarness, { binding: value });
    await waitFor(() => expect(cells(view.container).length).toBeGreaterThan(0));
    await fireEvent(cells(view.container)[0], pointer('pointerdown', 150, 160));
    const next = { ...value, ...(change === 'source' ? { source: { ...value.source, source_token: 'replaced-source-token' } }
      : change === 'context' ? { context: { kind: 'chat' as const, conversation_id: 'replacement-chat' } }
        : change === 'mode' ? { mode: 'view' as const }
          : { source: { ...value.source, capabilities: { ...value.source.capabilities, comment: false } } }) };
    await view.rerender({ binding: next });
    expect(view.container.querySelectorAll('[data-feedback-table-selected]')).toHaveLength(0);
    expect(value.onTargetChange).toHaveBeenLastCalledWith(null);
    expect(value.onInteractionChange).toHaveBeenLastCalledWith(false);
  });
  it('rejects a range containing hidden cells before publishing a candidate', async () => {
    const value = binding(), view = render(TableRangeHarness, { binding: value });
    await waitFor(() => expect(cells(view.container).length).toBeGreaterThan(0));
    for (const cell of view.container.querySelectorAll('tbody tr:nth-child(2) td'))
      cell.getBoundingClientRect = () => new DOMRect(0, 0, 0, 0);
    const all = cells(view.container);
    await drag([all[0], all[4]], [[150, 160], [150, 240]]);
    expect(vi.mocked(value.onTargetChange!).mock.calls.some(([target]) => target?.candidate.locator.kind === 'rich' && target.candidate.locator.table_range?.rows.includes(1))).toBe(false);
    expect(screen.getByRole('alert').textContent).toMatch(/hidden|locat/);
  });
  it('supports matrix data cells without including generated evidence cells or rows', async () => {
    const value = binding(), view = render(TableRangeHarness, { binding: value, block: {
      id: 'decision-data', type: 'decision_matrix', columns: ['name', 'count'],
      rows: [{ name: 'Alpha', count: 3, evidence: [{ title: 'Canonical source', text: 'More context' }] }, { name: 'Beta', count: 4 }],
    } });
    await waitFor(() => expect(cells(view.container).length).toBeGreaterThan(0));
    const all = cells(view.container);
    await drag([all[0], all[1]], [[150, 160], [250, 160]]);
    await fireEvent.click(screen.getByRole('button', { name: 'Show evidence' }));
    expect(target(value).geometry()?.rects).toHaveLength(2);
    expect(view.container.querySelectorAll('[data-feedback-table-selected]')).toHaveLength(2);
    const invalid: FeedbackLocator = { ...target(value).candidate.locator as Extract<FeedbackLocator, { kind: 'rich' }>,
      table_range: { representation: 'rich-table-v1', scope: 'cells', rows: [0], column_start: 2, column_end: 3 } };
    expect(adapter(value).targetGeometry?.(invalid)).toBeNull();
  });
});
