import type { ChartConfiguration } from 'chart.js';

import { neutralChartConfig as buildNeutralChartConfig } from './rich-data';

export type RichBlock = Record<string, unknown> & { type?: string; kind?: string; blocks?: unknown[] };

export const SUPPORTED_RICH_BLOCK_TYPES = new Set([
  'hero',
  'section',
  'stack',
  'columns',
  'grid',
  'tabs',
  'accordion',
  'modal',
  'markdown',
  'callout',
  'card',
  'card_grid',
  'dashboard',
  'status',
  'status_grid',
  'action',
  'metric',
  'kv',
  'key_value',
  'timeline',
  'steps',
  'day_agenda',
  'incident_timeline',
  'incident_checklist',
  'checklist',
  'quote',
  'divider',
  'figure',
  'gallery',
  'table',
  'comparison_matrix',
  'decision_matrix',
  'research_answer',
  'evidence_report',
  'claim_cards',
  'chart',
  'mermaid',
  'link',
  'link_preview',
  'source_list',
  'code',
]);

export interface RichDeliverablePayload {
  blocks: RichBlock[];
  assets: Record<string, unknown>[];
  sources: Record<string, unknown>[];
  datasets: Record<string, unknown>[];
  exports: Record<string, unknown>[];
  metadata: Record<string, unknown>;
  media_manifest?: Record<string, Record<string, unknown>>;
}

export type RichMediaUrlFor = (mediaKey: string) => string;

export function privateDeliverableMediaUrl(
  deliverableId: string,
  mediaKey: string,
  accessorConversationId = '',
): string {
  const query = accessorConversationId
    ? `?accessor_conversation_id=${encodeURIComponent(accessorConversationId)}`
    : '';
  return `/api/v1/deliverables/${encodeURIComponent(deliverableId)}/media/${encodeURIComponent(mediaKey)}${query}`;
}

export type RichPresentation = 'default' | 'pulse';

export function richPresentation(metadata: Record<string, unknown>): RichPresentation {
  return metadata.presentation === 'pulse' ? 'pulse' : 'default';
}

export type RichDensity = 'airy' | 'dense';

/** Block types that read as "scan quickly" status-at-a-glance content
 * (dashboards, RCA/ops archetypes) rather than long-form reading
 * (research, publication, notes archetypes). Used only to pick a spacing
 * rhythm (tighter for dense, more generous for airy) -- never to change
 * typography or per-block treatment. */
const DENSITY_SIGNAL_TYPES = new Set([
  'dashboard', 'status', 'status_grid', 'metric', 'kv', 'key_value',
  'table', 'comparison_matrix', 'decision_matrix', 'chart', 'incident_timeline',
  'incident_checklist', 'checklist',
]);

/** Density is a spacing-rhythm heuristic, not an authoring requirement:
 * an explicit `metadata.density` always wins. Otherwise, count
 * density-signal block types (recursively, including nested/grid
 * children) against the total block count. A composition dominated by
 * dashboards/metrics/tables/status blocks reads as "dense" (tighter
 * rhythm); a composition dominated by prose/research/narrative blocks
 * stays "airy" (the default, more generous rhythm). Requires a minimum
 * absolute count so a single metric in an otherwise long-form document
 * doesn't flip the whole document dense. */
export function richDensity(metadata: Record<string, unknown>, blocks: RichBlock[]): RichDensity {
  if (metadata.density === 'dense' || metadata.density === 'airy') return metadata.density;
  let total = 0;
  let signals = 0;
  const visit = (block: RichBlock) => {
    total += 1;
    if (DENSITY_SIGNAL_TYPES.has(blockType(block))) signals += 1;
    for (const child of blockChildren(block)) visit(child);
  };
  for (const block of blocks) visit(block);
  if (total === 0) return 'airy';
  return signals >= 3 && signals / total >= 0.35 ? 'dense' : 'airy';
}

export function safeUrl(value: unknown): string {
  if (typeof value !== 'string') return '';
  try {
    const base = typeof window !== 'undefined' ? window.location.origin : 'https://cognis.local';
    const url = new URL(value, base);
    return ['http:', 'https:', 'mailto:'].includes(url.protocol) ? url.href : '';
  } catch {
    return '';
  }
}

export function safeImageUrl(value: unknown): string {
  if (typeof value !== 'string') return '';
  const source = value.trim();
  if (/^data:image\/(?:png|jpeg|gif|webp);base64,[a-z0-9+/=\s]+$/i.test(source)) return source;
  if (source.toLowerCase().startsWith('data:image/svg+xml,')) {
    try {
      const svg = decodeURIComponent(source.slice(source.indexOf(',') + 1)).trim();
      const unsafe = !svg.toLowerCase().startsWith('<svg')
        || svg.length > 100_000
        || /<(?:script|foreignobject|image|use)\b|\bon[a-z]+\s*=|\b(?:href|xlink:href)\s*=|url\s*\(/i.test(svg);
      return unsafe ? '' : source;
    } catch {
      return '';
    }
  }
  return safeUrl(source);
}

export function normalizeRichDeliverable(value: unknown): RichDeliverablePayload {
  const raw = value && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : {};
  const list = (key: string): Record<string, unknown>[] =>
    Array.isArray(raw[key]) ? (raw[key] as unknown[]).filter((item): item is Record<string, unknown> => Boolean(item) && typeof item === 'object' && !Array.isArray(item)) : [];
  const blocks = Array.isArray(raw.blocks)
    ? raw.blocks.map((block) => block && typeof block === 'object' && !Array.isArray(block) ? block as RichBlock : { type: 'unknown', raw: block })
    : [];
  return {
    blocks,
    assets: list('assets'),
    sources: list('sources'),
    datasets: list('datasets'),
    exports: list('exports'),
    metadata: raw.metadata && typeof raw.metadata === 'object' && !Array.isArray(raw.metadata) ? raw.metadata as Record<string, unknown> : {},
    media_manifest: raw.media_manifest && typeof raw.media_manifest === 'object' && !Array.isArray(raw.media_manifest)
      ? Object.fromEntries(Object.entries(raw.media_manifest as Record<string, unknown>)
        .filter((entry): entry is [string, Record<string, unknown>] =>
          Boolean(entry[1]) && typeof entry[1] === 'object' && !Array.isArray(entry[1])))
      : {},
  };
}

export function resolveRichMedia(
  payload: RichDeliverablePayload,
  mediaUrlFor: RichMediaUrlFor,
): RichDeliverablePayload {
  const manifest = payload.media_manifest ?? {};
  if (Object.keys(manifest).length === 0) return payload;
  const resolveBlock = (block: RichBlock): RichBlock => {
    const resolved = { ...block };
    const media = block.media && typeof block.media === 'object' && !Array.isArray(block.media)
      ? block.media as Record<string, unknown>
      : null;
    const key = typeof media?.key === 'string' ? media.key : '';
    if (/^media_[0-9a-f]{24}$/.test(key) && manifest[key]) {
      const src = mediaUrlFor(key);
      if (src) {
        resolved.media = {
          ...media,
          src,
        };
      }
    }
    for (const childKey of ['blocks', 'children', 'items'] as const) {
      const children = block[childKey];
      if (Array.isArray(children)) {
        resolved[childKey] = children.map((child) =>
          child && typeof child === 'object' && !Array.isArray(child)
            ? resolveBlock(child as RichBlock)
            : child);
      }
    }
    return resolved;
  };
  return {
    ...payload,
    blocks: payload.blocks.map(resolveBlock),
  };
}

export function blockText(block: Record<string, unknown>, key = 'content'): string {
  const value = block[key];
  return typeof value === 'string' ? value : '';
}

/**
 * Body text for blocks with no separate summary/dek display slot (e.g.
 * callout). Authors frequently use `summary`/`dek`/`description` instead of
 * `content` -- these must never be silently dropped. Only use this for
 * blocks where `summary`/`dek`/`description` aren't already rendered
 * elsewhere, or the same text would appear twice.
 */
export function blockBody(block: Record<string, unknown>): string {
  return (
    blockText(block, 'content')
    || blockText(block, 'summary')
    || blockText(block, 'dek')
    || blockText(block, 'description')
  );
}

/**
 * Secondary description text with the same summary/dek fallback chain, for
 * blocks that already render `content` separately (metric, dashboard) and
 * only need the explanatory line to accept the wider vocabulary.
 */
export function blockDescription(block: Record<string, unknown>): string {
  return (
    blockText(block, 'description')
    || blockText(block, 'summary')
    || blockText(block, 'dek')
  );
}

export function blockType(block: Record<string, unknown>): string {
  return String(block.type ?? block.kind ?? 'unknown');
}

export function blockTitle(block: Record<string, unknown>): string {
  return blockText(block, 'title') || blockText(block, 'label') || blockText(block, 'name');
}

/**
 * Resolves a block's media/image reference. `media` is the one canonical
 * field, the same shape used directly by CardBlock/FigureBlock -- most
 * usefully for a hero block wrapping an agent-generated banner image as
 * the report's lead visual. Deliberately does not accept aliases (`image`/
 * `banner`/etc.): the Python standalone/PDF renderer's `_render_card_media`
 * only resolves `block["media"]`, so a second accepted field name here
 * would silently lose the banner in that renderer.
 */
export function blockMedia(block: Record<string, unknown>): unknown {
  return block.media;
}

export function blockChildren(block: Record<string, unknown>): RichBlock[] {
  const type = blockType(block);
  const itemBackedBlocks = new Set(['accordion', 'tabs', 'modal', 'gallery']);
  const explicitChildren = Array.isArray(block.blocks) ? block.blocks : block.children;
  const renderedChildren = [
    ...(Array.isArray(explicitChildren) ? explicitChildren : []),
    ...(itemBackedBlocks.has(type) && Array.isArray(block.items) ? block.items : []),
  ];
  return renderedChildren.map((child) =>
    child && typeof child === 'object' && !Array.isArray(child)
      ? child as RichBlock
      : { type: 'unknown', raw: child }
  );
}

export function richBlockRenderPlan(payload: unknown): { type: string; title: string; fallback: boolean }[] {
  const normalized = normalizeRichDeliverable(payload);
  const plan: { type: string; title: string; fallback: boolean }[] = [];
  const visit = (block: RichBlock) => {
    const type = blockType(block);
    plan.push({ type, title: blockTitle(block), fallback: !SUPPORTED_RICH_BLOCK_TYPES.has(type) });
    for (const child of blockChildren(block)) visit(child);
  };
  for (const block of normalized.blocks) visit(block);
  return plan;
}

export function neutralChartConfig(block: Record<string, unknown>): ChartConfiguration | null {
  return buildNeutralChartConfig(block);
}

