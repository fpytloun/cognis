import type { FeedbackTargetGeometry } from './types';
import type { FeedbackOverlayBounds } from './overlay-bounds';

export type TargetRect = NonNullable<FeedbackTargetGeometry>['rects'][number];
export function intersects(a: TargetRect, b: TargetRect): boolean {
  return a.left < b.left + b.width && a.left + a.width > b.left && a.top < b.top + b.height && a.top + a.height > b.top;
}
export function visibleRects(rects: TargetRect[], bounds: FeedbackOverlayBounds): TargetRect[] {
  return rects.filter(rect => intersects(rect, { left: bounds.left, top: bounds.top, width: bounds.width, height: bounds.height }));
}
function overlapArea(a: TargetRect, b: TargetRect): number {
  const width = Math.min(a.left + a.width, b.left + b.width) - Math.max(a.left, b.left);
  const height = Math.min(a.top + a.height, b.top + b.height) - Math.max(a.top, b.top);
  return width > 0 && height > 0 ? width * height : 0;
}
/** Union only the paint. Thread identities and exact locators stay untouched. */
export function unionRects(rects: TargetRect[]): TargetRect[] {
  const result: TargetRect[] = [];
  for (const rect of [...rects].sort((a, b) => a.top - b.top || a.left - b.left)) {
    if (rect.width <= 0 || rect.height <= 0) continue;
    const line = result.find(value => Math.abs(value.top - rect.top) < .5 && Math.abs(value.height - rect.height) < .5
      && value.left <= rect.left + rect.width + .5 && rect.left <= value.left + value.width + .5);
    if (line) {
      const right = Math.max(line.left + line.width, rect.left + rect.width);
      line.left = Math.min(line.left, rect.left); line.width = right - line.left;
    } else result.push({ ...rect });
  }
  return result.sort((a, b) => a.top - b.top || a.left - b.left);
}
/**
 * Paint for the active target. Structural ranges (table cells, rows, elements) that densely
 * fill their bounding box read as one region; flowing text keeps per-line boxes.
 */
export function targetPaint(rects: TargetRect[]): { rects: TargetRect[]; block: boolean } {
  const lines = unionRects(rects);
  if (lines.length < 2) return { rects: lines, block: false };
  const left = Math.min(...lines.map(rect => rect.left)), top = Math.min(...lines.map(rect => rect.top));
  const right = Math.max(...lines.map(rect => rect.left + rect.width)), bottom = Math.max(...lines.map(rect => rect.top + rect.height));
  const box = { left, top, width: right - left, height: bottom - top };
  const filled = lines.reduce((sum, rect) => sum + rect.width * rect.height, 0);
  return filled >= box.width * box.height * .8 ? { rects: [box], block: true } : { rects: lines, block: false };
}

export interface ContentObstacles {
  /** Text line boxes, media and controls: nothing floating may sit on these. */
  ink: TargetRect[];
  /** Small rounded filled surfaces such as message bubbles and cards; cards prefer to avoid them. */
  surfaces: TargetRect[];
}
/**
 * Visible source content inside bounds. Placement treats it as obstacles so floating chrome never
 * covers what the reader is reading or typing into. Off-screen subtrees are skipped by element box.
 */
export function contentObstacles(bounds: FeedbackOverlayBounds, root: Node | null = typeof document === 'undefined' ? null : document.body): ContentObstacles {
  const result: ContentObstacles = { ink: [], surfaces: [] };
  if (!root || typeof document === 'undefined' || typeof document.createTreeWalker !== 'function') return result;
  // Without layout (e.g. a headless DOM) there is nothing to avoid.
  if (!document.documentElement.getBoundingClientRect().height) return result;
  const area = { left: bounds.left, top: bounds.top, width: bounds.width, height: bounds.height };
  const add = (list: TargetRect[], box: { left: number; top: number; width: number; height: number }) => {
    if (box.width > 0 && box.height > 0 && intersects(box, area)) list.push({ left: box.left, top: box.top, width: box.width, height: box.height });
  };
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_ELEMENT | NodeFilter.SHOW_TEXT, {
    acceptNode(node) {
      if (node.nodeType === Node.TEXT_NODE) return node.textContent?.trim() ? NodeFilter.FILTER_ACCEPT : NodeFilter.FILTER_SKIP;
      const element = node as Element;
      if (element.matches('[data-feedback-overlay], script, style, template, [hidden], [aria-hidden="true"]')) return NodeFilter.FILTER_REJECT;
      const box = element.getBoundingClientRect();
      // Unlaid-out trees (no geometry at all) cannot be covered; skip them wholesale.
      if (!box.width && !box.height && !box.left && !box.top && element.childElementCount === 0) return NodeFilter.FILTER_REJECT;
      if (box.width && box.height && !intersects(box, area)) return NodeFilter.FILTER_REJECT;
      if (element.matches('img, canvas, video, iframe, button, input, textarea, select, [contenteditable="true"]')) {
        add(result.ink, box); return NodeFilter.FILTER_REJECT;
      }
      // Only bubble/card-sized boxes can be surfaces; this keeps style reads to a few elements.
      if (box.width >= 160 && box.height >= 40 && box.width * box.height <= area.width * area.height / 4) {
        const style = getComputedStyle(element);
        const background = style.backgroundColor;
        const alpha = /rgba\([^)]*,\s*([\d.]+)\)/.exec(background)?.[1] ?? /\/\s*([\d.]+%?)\s*\)/.exec(background)?.[1];
        const opacity = alpha === undefined ? 1 : alpha.endsWith('%') ? parseFloat(alpha) / 100 : Number(alpha);
        const filled = background !== 'transparent' && opacity > .3;
        if (filled && parseFloat(style.borderTopLeftRadius) > 0) add(result.surfaces, box);
      }
      return NodeFilter.FILTER_SKIP;
    },
  });
  const range = document.createRange();
  for (let node = walker.nextNode(); node; node = walker.nextNode()) {
    range.selectNodeContents(node);
    for (const box of Array.from(typeof range.getClientRects === 'function' ? range.getClientRects() : [])) add(result.ink, box);
  }
  return result;
}

export interface FloatingOffset { dx: number; dy: number }
export interface FloatingPosition { left: number; top: number; width: number; height: number; docked: boolean; offset: FloatingOffset | null }
/**
 * Nearest position that covers the least source ink and never the target itself. A previous
 * offset relative to the target is kept while it stays clear, so typing does not make it jump.
 */
export function floatingPlacement(bounds: FeedbackOverlayBounds, rects: TargetRect[], size: { width: number; height: number }, narrow: boolean,
  obstacles: TargetRect[] = [], previous: FloatingOffset | null = null, surfaces: TargetRect[] = []): FloatingPosition {
  const width = Math.min(size.width, bounds.width), height = Math.min(size.height, bounds.height);
  const target = visibleRects(rects, bounds);
  const docked = !!rects.length && !target.length;
  const clamp = (left: number, top: number) => ({
    left: Math.max(bounds.left, Math.min(left, bounds.right - width)),
    top: Math.max(bounds.top, Math.min(top, bounds.bottom - height)), width, height,
  });
  if (narrow) return { ...clamp(bounds.right - width, bounds.bottom - height), docked, offset: null };
  const anchor = target.length ? {
    left: Math.min(...target.map(rect => rect.left)), top: Math.min(...target.map(rect => rect.top)),
    right: Math.max(...target.map(rect => rect.left + rect.width)), bottom: Math.max(...target.map(rect => rect.top + rect.height)),
  } : { left: bounds.right, top: bounds.top, right: bounds.right, bottom: bounds.top };
  // A target's own line box also carries the text around it, so it stays an obstacle; the
  // target itself is excluded separately by `valid`.
  // Covering text is discouraged, but distance from the target matters more: a card that
  // partly covers nearby text beats one that jumps to the far edge of the view. Coverage is
  // scored as the covered share of the card, so it trades against distance in pixels.
  const area = Math.max(1, width * height);
  const cost = (candidate: TargetRect) => (obstacles.reduce((sum, rect) => sum + overlapArea(candidate, rect), 0) * 600
    + surfaces.reduce((sum, rect) => sum + overlapArea(candidate, rect), 0) * 60) / area;
  // The lines holding the selection stay readable, like the selection itself.
  const targetLines = obstacles.filter(rect => target.some(value => intersects(rect, value)));
  const gap = (candidate: TargetRect) => Math.hypot(
    Math.max(0, anchor.left - (candidate.left + candidate.width), candidate.left - anchor.right),
    Math.max(0, anchor.top - (candidate.top + candidate.height), candidate.top - anchor.bottom));
  const valid = (candidate: TargetRect) => !target.some(rect => intersects(candidate, rect))
    && !targetLines.some(rect => intersects(candidate, rect));
  const done = (candidate: { left: number; top: number; width: number; height: number }): FloatingPosition =>
    ({ ...candidate, docked, offset: target.length ? { dx: candidate.left - anchor.left, dy: candidate.top - anchor.top } : null });
  if (previous && target.length) {
    const kept = clamp(anchor.left + previous.dx, anchor.top + previous.dy);
    if (kept.left === anchor.left + previous.dx && kept.top === anchor.top + previous.dy && valid(kept) && !cost(kept)) return done(kept);
  }
  const lefts = new Set<number>(), tops = new Set<number>();
  for (let left = bounds.left; left <= bounds.right - width; left += 24) lefts.add(Math.round(left));
  for (let top = bounds.top; top <= bounds.bottom - height; top += 24) tops.add(Math.round(top));
  for (const left of [anchor.right + 16, anchor.left - width - 16, anchor.left, bounds.right - width]) lefts.add(clamp(left, 0).left);
  // Just past the right edge of nearby text is often the closest clear column.
  for (const rect of obstacles) if (Math.abs(rect.top - anchor.top) < height * 2) lefts.add(clamp(Math.round(rect.left + rect.width + 8), 0).left);
  for (const top of [anchor.top, anchor.bottom + 8, anchor.top - height - 8, bounds.bottom - height]) tops.add(clamp(0, top).top);
  let best: TargetRect | null = null, score = Infinity;
  for (const left of lefts) for (const top of tops) {
    const candidate = { left, top, width, height };
    if (!valid(candidate)) continue;
    // Covered ink dominates; among equally clear spots the nearest wins, preferring right and below.
    const value = cost(candidate) + gap(candidate) + (left < anchor.left ? 24 : 0) + (top < anchor.top ? 12 : 0);
    if (value < score) { score = value; best = candidate; }
  }
  return done(best ?? clamp(anchor.left, anchor.bottom + 8));
}

export interface AvatarTarget { id: string; author: string; guest: boolean; label: string; rects: TargetRect[] }
export interface AvatarCluster { ids: string[]; author: string; guest: boolean; label: string; left: number; top: number }
/** Marker hit areas are 44px squares around a 20px visible marker. */
export const MARKER_HIT = 44, MARKER_SIZE = 20;
/**
 * Markers sit on their target's first line, in the first gap after the target that holds no
 * source ink (else the gap before it). They are never placed between lines or over text.
 */
export function avatarClusters(items: AvatarTarget[], bounds: FeedbackOverlayBounds, obstacles: TargetRect[] = []): AvatarCluster[] {
  const clusters: AvatarCluster[] = [];
  const inset = (MARKER_HIT - MARKER_SIZE) / 2;
  const clear = (left: number, middle: number) => {
    const band = { left: left + inset - 2, top: middle - 6, width: MARKER_SIZE + 4, height: 12 };
    return band.left >= bounds.left && band.left + band.width <= bounds.right && !obstacles.some(rect => intersects(rect, band));
  };
  for (const item of items) {
    const visible = visibleRects(item.rects, bounds);
    const first = visible[0];
    if (!first) continue;
    const line = visible.filter(rect => Math.abs(rect.top - first.top) < 1);
    const right = Math.max(...line.map(rect => rect.left + rect.width)), left = Math.min(...line.map(rect => rect.left));
    const middle = first.top + Math.min(first.height, 32) / 2;
    if (middle - MARKER_SIZE / 2 < bounds.top || middle + MARKER_SIZE / 2 > bounds.bottom) continue;
    let slot: number | null = null;
    for (let x = right + 4 - inset; x + inset + MARKER_SIZE <= bounds.right; x += 8) if (clear(x, middle)) { slot = x; break; }
    if (slot === null) for (let x = left - 4 - MARKER_SIZE - inset; x + inset >= bounds.left; x -= 8) if (clear(x, middle)) { slot = x; break; }
    if (slot === null) continue;
    const rect = { left: slot, top: middle - MARKER_HIT / 2, width: MARKER_HIT, height: MARKER_HIT };
    const collision = clusters.find(cluster => intersects({ ...cluster, width: MARKER_HIT, height: MARKER_HIT }, rect));
    if (collision) { collision.ids.push(item.id); collision.guest ||= item.guest; }
    else clusters.push({ ids: [item.id], author: item.author, guest: item.guest, label: item.label, left: rect.left, top: rect.top });
  }
  return clusters;
}
export function authorInitials(label: string): string {
  return label.trim().split(/\s+/u).filter(Boolean).slice(0, 2).map(word => Array.from(word)[0]).join('').toLocaleUpperCase() || '?';
}
