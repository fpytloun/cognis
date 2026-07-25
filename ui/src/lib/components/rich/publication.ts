import { blockChildren, blockText, blockTitle, blockType, richPresentation, safeUrl, type RichBlock } from '$lib/rich-deliverable';
import {
  normalizeDoi,
  normalizedSourceIdentity,
  normalizeSources,
  resolveSourceRefs,
  type NormalizedSource,
} from './evidence-helpers';

export interface TocItem {
  anchor: string;
  requestedAnchor: string;
  label: string;
  level: 2 | 3 | 4;
  block: RichBlock;
  markdownIndex?: number;
}

export interface TocNode {
  item: TocItem;
  children: TocNode[];
}

export interface PublicationOptions {
  showToc: boolean;
  tocDepth: 2 | 3 | 4;
  numberFigures: boolean;
  numberTables: boolean;
}

export interface CitationRegistry {
  numbers: Record<string, number>;
  sources: NormalizedSource[];
  namespace: string;
}

function boolValue(value: unknown): boolean | undefined {
  return typeof value === 'boolean' ? value : undefined;
}

function contentSubstance(value: unknown): { characters: number; structures: number } {
  if (typeof value === 'string') return { characters: value.trim().length, structures: 0 };
  if (Array.isArray(value)) {
    return value.reduce(
      (total, item) => {
        const nested = contentSubstance(item);
        return {
          characters: total.characters + nested.characters,
          structures: total.structures + nested.structures,
        };
      },
      { characters: 0, structures: 0 },
    );
  }
  if (!value || typeof value !== 'object') return { characters: 0, structures: 0 };
  const record = value as Record<string, unknown>;
  const ownStructure = ['table', 'comparison_matrix', 'decision_matrix', 'figure', 'chart', 'code', 'mermaid']
    .includes(String(record.type ?? '')) ? 1 : 0;
  return Object.entries(record).reduce(
    (total, [key, item]) => {
      if (['id', 'anchor', 'type', 'variant', 'url', 'src'].includes(key)) return total;
      const nested = contentSubstance(item);
      return {
        characters: total.characters + nested.characters,
        structures: total.structures + nested.structures,
      };
    },
    { characters: 0, structures: ownStructure },
  );
}

export function isSubstantialDocument(blocks: RichBlock[], topLevelHeadings: number): boolean {
  if (topLevelHeadings < 4) return false;
  const substance = contentSubstance(blocks);
  return substance.characters >= 4_000
    || (substance.characters >= 2_400 && substance.structures >= 2)
    || (substance.characters >= 1_500 && substance.structures >= 4)
    || (topLevelHeadings >= 10 && substance.characters >= 3_000);
}

export function publicationOptions(metadata: Record<string, unknown>, blocks: RichBlock[]): PublicationOptions {
  const presentation = richPresentation(metadata);
  const toc = metadata.toc;
  const tocRecord = toc && typeof toc === 'object' && !Array.isArray(toc) ? toc as Record<string, unknown> : {};
  const requestedDepth = tocRecord.depth ?? metadata.toc_depth;
  const tocDepth: 2 | 3 | 4 = requestedDepth === 4 ? 4 : requestedDepth === 3 ? 3 : 2;
  const publication = metadata.publication;
  const publicationRecord = publication && typeof publication === 'object' && !Array.isArray(publication)
    ? publication as Record<string, unknown>
    : {};
  const tocItems = buildTocItems(blocks, tocDepth);
  const override = boolValue(toc) ?? boolValue(tocRecord.enabled) ?? boolValue(metadata.show_toc);
  const publicationDefault = publication === true;
  const topLevelHeadings = tocItems.filter((item) => item.level === 2).length;
  return {
    showToc: presentation === 'pulse' ? false : override ?? isSubstantialDocument(blocks, topLevelHeadings),
    tocDepth,
    numberFigures: presentation === 'pulse' ? false : boolValue(publicationRecord.number_figures) ?? boolValue(metadata.number_figures) ?? publicationDefault,
    numberTables: presentation === 'pulse' ? false : boolValue(publicationRecord.number_tables) ?? boolValue(metadata.number_tables) ?? publicationDefault,
  };
}

function slugBase(value: string): string {
  return value
    .normalize('NFKD')
    .replace(/[\u0300-\u036f]/g, '')
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-|-$/g, '') || 'section';
}

const RESERVED_ID_PREFIXES = [
  'rich-section-',
  'cite-',
  'citation-',
  'rich-citation-',
  'reference-',
  'toc-',
  'figure-',
  'table-',
  'mermaid-',
];
const RESERVED_IDS = new Set(['references-heading', 'toc']);

function createIdAllocator() {
  const used = new Set<string>();
  return (value: string, fallback = 'section'): string => {
    let base = slugBase(value || fallback);
    if (RESERVED_IDS.has(base) || RESERVED_ID_PREFIXES.some((prefix) => base.startsWith(prefix))) {
      base = `section-${base}`;
    }
    let candidate = base;
    let suffix = 2;
    while (used.has(candidate)) candidate = `${base}-${suffix++}`;
    used.add(candidate);
    return candidate;
  };
}

interface MarkdownHeading {
  index: number;
  label: string;
  sourceLevel: number;
}

function markdownHeadings(block: RichBlock): MarkdownHeading[] {
  return Array.from(blockText(block).matchAll(/^(#{1,4})\s+(.+?)\s*#*\s*$/gm))
    .map((match, index) => ({
      index,
      label: match[2].trim(),
      sourceLevel: match[1].length,
    }))
    .filter((heading) => Boolean(heading.label));
}

function navigationTitle(block: RichBlock): string {
  const title = blockTitle(block);
  if (title || blockType(block) !== 'markdown') return title;
  return markdownHeadings(block)[0]?.label ?? '';
}

function nestedMarkdownLevel(
  heading: MarkdownHeading,
  blockLevel: 2 | 3 | 4,
  firstSourceLevel: number,
  explicitTitle: boolean,
): 3 | 4 {
  const relative = explicitTitle
    ? heading.sourceLevel
    : Math.max(1, heading.sourceLevel - firstSourceLevel);
  return Math.min(4, blockLevel + relative) as 3 | 4;
}

export function buildTocItems(blocks: RichBlock[], depth: 2 | 3 | 4 = 2): TocItem[] {
  const allocateId = createIdAllocator();
  const items: TocItem[] = [];
  const visit = (current: RichBlock[], level: 2 | 3 | 4) => {
    for (const block of current) {
      const title = navigationTitle(block);
      const type = blockType(block);
      if (title && type !== 'hero' && type !== 'divider' && level <= depth) {
        const requestedAnchor = slugBase(String(block.id ?? block.anchor ?? title));
        const anchor = allocateId(requestedAnchor);
        items.push({ anchor, requestedAnchor, label: title, level, block });
        if (type === 'markdown' && depth > level) {
          const headings = markdownHeadings(block);
          const start = blockTitle(block) ? 0 : 1;
          const firstSourceLevel = headings[0]?.sourceLevel ?? 1;
          headings.slice(start).forEach((heading) => {
            const nestedLevel = nestedMarkdownLevel(
              heading,
              level,
              firstSourceLevel,
              Boolean(blockTitle(block)),
            );
            if (nestedLevel > depth) return;
            items.push({
              anchor: allocateId(heading.label),
              requestedAnchor: slugBase(heading.label),
              label: heading.label,
              level: nestedLevel,
              block,
              markdownIndex: heading.index,
            });
          });
        }
      }
      if (level < depth) {
        visit(publicationChildren(block), Math.min(4, level + 1) as 3 | 4);
      }
    }
  };
  visit(blocks, 2);
  return items;
}

export function nestTocItems(items: TocItem[]): TocNode[] {
  const roots: TocNode[] = [];
  const stack: TocNode[] = [];
  for (const item of items) {
    const node: TocNode = { item, children: [] };
    while (stack.length > 0 && stack[stack.length - 1].item.level >= item.level) stack.pop();
    const parent = stack[stack.length - 1];
    (parent?.children ?? roots).push(node);
    stack.push(node);
  }
  return roots;
}

export function namespaceTocItems(items: TocItem[], namespace: string): TocItem[] {
  return items.map((item) => ({ ...item, anchor: `${namespace}-${item.anchor}` }));
}

function publicationChildren(block: RichBlock): RichBlock[] {
  const children = block.blocks ?? block.children;
  return Array.isArray(children)
    ? children.map((child) =>
      child && typeof child === 'object' && !Array.isArray(child)
        ? child as RichBlock
        : { type: 'unknown', raw: child }
    )
    : [];
}

export function decorateBlocks(
  blocks: RichBlock[],
  items: TocItem[],
  options?: PublicationOptions,
  namespace = ''
): RichBlock[] {
  let figureNumber = 0;
  let tableNumber = 0;
  let blockNumber = 0;
  let mermaidNumber = 0;
  const linkTargets: Record<string, string> = {};
  for (const item of items) linkTargets[item.requestedAnchor] ??= item.anchor;
  const visit = (block: RichBlock, legacyAnchor = ''): RichBlock => {
    const copy: RichBlock = { ...block };
    copy.__publication_link_targets = linkTargets;
    for (const key of ['href', 'url'] as const) {
      const value = block[key];
      if (typeof value === 'string' && value.startsWith('#')) {
        const target = linkTargets[slugBase(value.slice(1))];
        if (target) copy[key] = `#${target}`;
      }
    }
    const blockItems = items.filter((item) => item.block === block);
    const primary = blockItems.find((item) => item.markdownIndex === undefined);
    if (primary) {
      copy.__publication_anchor = primary.anchor;
      copy.__publication_level = primary.level;
      copy.__publication_markdown_headings = blockItems
        .filter((item) => item.markdownIndex !== undefined)
        .map((item) => ({
          anchor: item.anchor,
          index: item.markdownIndex,
          level: item.level,
          title: item.label,
        }));
    }
    if (legacyAnchor) copy.__legacy_anchor = namespace ? `${namespace}-${legacyAnchor}` : legacyAnchor;
    copy.__publication_block = ++blockNumber;
    if (blockType(block) === 'mermaid') {
      copy.__publication_mermaid_id = namespace
        ? `${namespace}-mermaid-${mermaidNumber++}`
        : `mermaid-${mermaidNumber++}`;
    }
    if (options?.numberFigures && blockType(block) === 'figure') copy.__figure_number = ++figureNumber;
    if (options?.numberTables && ['table', 'comparison_matrix', 'decision_matrix'].includes(blockType(block))) {
      copy.__table_number = ++tableNumber;
    }
    for (const key of ['blocks', 'children'] as const) {
      if (Array.isArray(block[key])) {
        copy[key] = (block[key] as unknown[]).map((child) =>
          child && typeof child === 'object' && !Array.isArray(child) ? visit(child as RichBlock) : child
        );
      }
    }
    if (['tabs', 'accordion', 'modal', 'gallery'].includes(blockType(block))) {
      for (const key of ['items', 'data', 'entries'] as const) {
        if (Array.isArray(block[key])) {
          copy[key] = (block[key] as unknown[]).map((child) =>
            child && typeof child === 'object' && !Array.isArray(child) ? visit(child as RichBlock) : child
          );
        }
      }
    }
    return copy;
  };
  return blocks.map((block, index) => visit(block, `rich-section-${index}`));
}

export function sourceIdentity(source: NormalizedSource): string {
  return normalizedSourceIdentity(source);
}

export { normalizeDoi };

export function orderedSources(value: unknown): NormalizedSource[] {
  const seen = new Set<string>();
  return normalizeSources(value).filter((source) => {
    const identity = sourceIdentity(source);
    if (seen.has(identity)) return false;
    seen.add(identity);
    return true;
  });
}

export function sourceDetails(source: NormalizedSource): string {
  const authors = Array.isArray(source.raw.authors)
    ? source.raw.authors.map(String).join(', ')
    : String(source.raw.authors ?? source.raw.author ?? '');
  const publication = String(source.raw.publication ?? source.publisher ?? '');
  const year = String(source.raw.year ?? source.date ?? '');
  const doi = normalizeDoi(source.raw.doi);
  const accessed = String(source.raw.accessed ?? source.raw.accessed_at ?? '');
  return [authors, publication, year, doi ? `doi: ${doi}` : '', accessed ? `accessed ${accessed}` : '']
    .filter(Boolean)
    .join(' · ');
}

export function safeSourceUrl(source: NormalizedSource): string {
  const doi = normalizeDoi(source.raw.doi);
  return doi ? safeUrl(`https://doi.org/${doi}`) : source.url;
}

function citationRefs(item: Record<string, unknown>): unknown {
  return item.source_ids ?? item.citations ?? item.sources;
}

export function buildCitationRegistry(blocks: RichBlock[], sources: unknown, namespace = ''): CitationRegistry {
  const canonical = orderedSources(sources);
  const ordered: NormalizedSource[] = [];
  const numbers: Record<string, number> = {};
  const add = (refs: unknown, available: NormalizedSource[]) => {
    for (const source of resolveSourceRefs(refs, available)) {
      const identity = sourceIdentity(source);
      if (numbers[identity] !== undefined) continue;
      numbers[identity] = ordered.length + 1;
      ordered.push(source);
    }
  };
  const visit = (items: RichBlock[]) => {
    for (const block of items) {
      const scoped = block.sources === undefined
        ? []
        : resolveSourceRefs(block.sources, canonical);
      const available = scoped.length > 0 ? orderedSources(scoped.map((source) => source.raw)) : canonical;
      if (blockType(block) === 'research_answer') {
        add(block.source_ids ?? block.citations, available);
        const paragraphs = Array.isArray(block.paragraphs ?? block.items) ? (block.paragraphs ?? block.items) as Record<string, unknown>[] : [];
        for (const paragraph of paragraphs) add(citationRefs(paragraph), available);
      }
      if (['comparison_matrix', 'decision_matrix'].includes(blockType(block))) {
        const rows = Array.isArray(block.rows ?? block.data) ? (block.rows ?? block.data) as Record<string, unknown>[] : [];
        for (const row of rows) add(citationRefs(row), available);
      }
      if (['evidence_report', 'claim_cards'].includes(blockType(block))) {
        const claims = Array.isArray(block.claims ?? block.items ?? block.data) ? (block.claims ?? block.items ?? block.data) as Record<string, unknown>[] : [];
        for (const claim of claims) add(citationRefs(claim), available);
      }
      visit(blockChildren(block));
    }
  };
  visit(blocks);
  return { numbers, sources: ordered, namespace };
}

export function citationNumber(registry: CitationRegistry, source: NormalizedSource): number {
  return registry.numbers[sourceIdentity(source)] ?? 0;
}
