import { mapRenderedMarkdown } from '$lib/markdown';
import { canonicalText, selectText } from './annotations/text';
import { textDigest } from './annotations/digest';
import { nativeSelection, clearNativeSelection, rangeGeometry } from './annotations/native-selection';
import { surfaceIdentity } from './annotations/surface-identity';
import { exposeGeometry } from './annotations/expose-target';
import type { FeedbackLocator, FeedbackSurfaceBinding } from './types';
import type { MarkdownSourceMap } from '$lib/markdown-provenance';
import { documentCoordination } from '$lib/document-coordination';

const controls = 'button,input,textarea,select,[data-copy-exclude]';
/** Ignore only renderer-owned controls; never turn their labels into source text. */
function bodyNodes(root: HTMLElement) {
  const nodes: Text[] = [];
  const walker = root.ownerDocument.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  for (let node = walker.nextNode(); node; node = walker.nextNode()) {
    if (!node.parentElement?.closest(controls)) nodes.push(node as Text);
  }
  return nodes;
}
function bodyText(root: HTMLElement) { return bodyNodes(root).map(node => node.data).join(''); }
function sameNodes(left: Text[], right: Text[]) {
  return left.length === right.length && left.every((node, index) => node === right[index]);
}
function bodyRange(root: HTMLElement, start: number, end: number) {
  let offset = 0;
  const range = root.ownerDocument.createRange();
  let found = false;
  for (const node of bodyNodes(root)) {
    if (!found && start < offset + node.length) { range.setStart(node, start - offset); found = true; }
    if (found && end <= offset + node.length) { range.setEnd(node, end - offset); return range; }
    offset += node.length;
  }
  return null;
}
function offsets(root: HTMLElement, range: Range) {
  if (!root.contains(range.startContainer) || !root.contains(range.endContainer)) return null;
  const inControl = (node: Node) => (node.nodeType === Node.ELEMENT_NODE ? node as Element : node.parentElement)?.closest(controls);
  if (inControl(range.startContainer) || inControl(range.endContainer)) return null;
  let offset = 0, start = -1, end = -1;
  for (const node of bodyNodes(root)) {
    // Browsers can place selection handles on paragraph/inline element boundaries. Count
    // intersections in the same control-free text projection used by the Markdown map,
    // rather than rejecting every element boundary when an unrelated Copy button exists.
    if (range.intersectsNode(node)) {
      const from = node === range.startContainer ? range.startOffset : 0;
      const to = node === range.endContainer ? range.endOffset : node.length;
      if (to > from) {
        if (start < 0) start = offset + from;
        end = offset + to;
      }
    }
    offset += node.length;
  }
  return start >= 0 && end > start ? { start, end } : null;
}
export function mountChatTextAdapter(root: HTMLElement, text: string, initial: FeedbackSurfaceBinding, error: (message: string) => void = () => {}) {
  let binding: FeedbackSurfaceBinding | null | undefined = initial, alive = true;
  const instance = crypto.randomUUID(), source = canonicalText(text);
  let mapped: MarkdownSourceMap | undefined;
  let mapping: Promise<MarkdownSourceMap> | undefined;
  let digest: string | undefined;
  let generation = 0, mappedIdentity = '';
  const lease = documentCoordination().capture();
  let unregister: (() => void) | undefined;
  let registering: number | undefined;
  function identity(value: FeedbackSurfaceBinding | null | undefined) {
    return JSON.stringify([surfaceIdentity(value), value?.source.subject_id, value?.source.content_digest,
      value?.source.capabilities, value?.source.availability, value?.source.canonical_text,
      value?.source.permitted_locator_kinds, value?.supported_locator_kinds, value?.completeness]);
  }
  let owner = identity(binding), lifetimeNodes = bodyNodes(root), mappedNodes: Text[] | undefined;
  async function exactMap() {
    const version = generation, expected = owner, nodes = bodyNodes(root);
    lease.assertCurrent();
    mapping ??= mapRenderedMarkdown(source);
    const result = await mapping;
    digest ??= await textDigest(source);
    lease.assertCurrent();
    if (!alive || generation !== version || expected !== identity(binding) || !root.isConnected
      || !sameNodes(nodes, bodyNodes(root)) || bodyText(root) !== result.text) throw new Error('This rendered text no longer matches its source.');
    const descriptor = binding?.source.canonical_text;
    if (!descriptor || descriptor.text_digest !== digest || descriptor.length !== source.length) throw new Error('This message does not match the exact canonical source.');
    mapped = result;
    mappedIdentity = expected;
    mappedNodes = nodes;
    return result;
  }
  function rangeFor(anchor: FeedbackLocator) {
    try { lease.assertCurrent(); } catch { return null; }
    if (!alive || !root.isConnected || mappedIdentity !== identity(binding) || !mappedNodes || !sameNodes(mappedNodes, bodyNodes(root))
      || anchor.kind !== 'text' || !mapped || !binding?.source.capabilities.read || bodyText(root) !== mapped.text
      || anchor.range.text_digest !== digest || source.slice(anchor.range.start, anchor.range.end) !== anchor.range.exact) return null;
    if (mapped.barriers.some(value => value.start < anchor.range.end && value.end > anchor.range.start)) return null;
    const start = mapped.units.findIndex(unit => unit?.start === anchor.range.start);
    let end = -1;
    mapped.units.forEach((unit, index) => { if (unit?.end === anchor.range.end) end = index + 1; });
    return start >= 0 && end > start ? bodyRange(root, start, end) : null;
  }
  async function map(range: Range) {
    const version = identity(binding), current = binding;
    const projection = await exactMap();
    if (!current || version !== identity(binding)) throw new Error('The feedback source changed.');
    const position = offsets(root, range);
    if (!position) throw new Error('Select only message text, not controls.');
    // Selections often start or end on rendered-only characters (spaces, list markers, quotes).
    // Trim them to the nearest text that exists in the message source instead of failing.
    while (position.start < position.end && !projection.units[position.start]) position.start++;
    while (position.end > position.start && !projection.units[position.end - 1]) position.end--;
    const first = projection.units[position.start], last = projection.units[position.end - 1];
    if (!first || !last || first.start >= last.end) throw new Error('Select message text to comment on.');
    if (projection.barriers.some(value => value.start < last.end && value.end > first.start)) {
      throw new Error('Select text without crossing generated or unmapped content.');
    }
    let previous = first.start;
    for (let index = position.start; index < position.end; index++) {
      const unit = projection.units[index];
      if (!unit && projection.text[index].trim()) throw new Error('The selection includes unmapped content.');
      if (unit && (unit.start < previous || unit.end > last.end)) throw new Error('This content cannot be mapped precisely.');
      if (unit) previous = unit.start;
    }
    const selected = selectText(source, first.start, last.end,
      current.source.canonical_text!.available_ranges.map(([start, end]) => ({ start, end })));
    return { candidate: { locator: { kind: 'text' as const, range: { representation: 'feedback-text-v1' as const,
      text_digest: digest!, ...selected } } }, label: range.toString() };
  }
  // The shared presenter owns visible errors; do not insert content into the source body.
  const selection = nativeSelection(root, { binding, instance, map, error });
  async function register() {
    const version = generation;
    if (!binding?.source.capabilities.read || registering === version) return;
    registering = version;
    try { await exactMap(); }
    catch (cause) {
      if (alive && version === generation) error(cause instanceof Error ? cause.message : 'This source cannot be mapped.');
      return;
    }
    if (!alive || version !== generation || !binding || owner !== identity(binding)) return;
    unregister = binding.registerAdapter(instance, {
      clearSelection: () => clearNativeSelection(root),
      targetGeometry: anchor => { const range = rangeFor(anchor); return range ? rangeGeometry(range) : null; },
      exposeTarget: async (anchor, visible) => { await exactMap(); exposeGeometry(root, () => { const range = rangeFor(anchor); return range ? rangeGeometry(range) : null; }, visible); },
      locate: async anchor => {
        try { await exactMap(); } catch { return { status: 'not_located' }; }
        const range = rangeFor(anchor);
        if (!range) return { status: 'not_located' };
        range.startContainer.parentElement?.scrollIntoView({ block: 'center' });
        return { status: 'exact' };
      },
    });
  }
  void register();
  return {
    update(next: FeedbackSurfaceBinding | null | undefined) {
      const nextOwner = identity(next), nodes = bodyNodes(root);
      const changed = nextOwner !== owner || !sameNodes(lifetimeNodes, nodes);
      // State/presenter snapshots create fresh binding objects for the same owner.
      // Only a material source/provenance/DOM change revokes that owner's work.
      if (changed) {
        generation++; unregister?.(); unregister = undefined; registering = undefined;
        owner = nextOwner; lifetimeNodes = nodes;
        mapped = undefined; mappedNodes = undefined; mappedIdentity = '';
      }
      binding = next;
      selection.update({ binding, instance, map, error });
      if (changed) void register();
    },
    destroy() { alive = false; generation++; selection.destroy(); unregister?.(); },
  };
}
