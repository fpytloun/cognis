import type { FeedbackEventRef, FeedbackLocator, FeedbackSourceRef, ResolvedFeedbackSource } from './types';

function sameEvent(a: FeedbackEventRef, b: FeedbackEventRef): boolean {
  return a.store === b.store && a.session_id === b.session_id && a.seq === b.seq
    && a.event_id === b.event_id && a.event_type === b.event_type;
}

/** Presentation comparison only; never grants access or remaps an anchor. */
export function sameFeedbackSource(a: FeedbackSourceRef, b: FeedbackSourceRef): boolean {
  if (a.kind === 'document') return b.kind === 'document' && a.content_ref === b.content_ref;
  if (a.kind === 'transcript') return b.kind === 'transcript' && a.conversation_id === b.conversation_id
    && sameEvent(a.source_event, b.source_event) && a.source_item_id === b.source_item_id
    && a.field_path.length === b.field_path.length && a.field_path.every((part, index) => part === b.field_path[index]);
  return b.kind === 'work_diff' && a.activity_scope_id === b.activity_scope_id
    && sameEvent(a.source_event, b.source_event) && a.source_item_id === b.source_item_id
    && a.file_ordinal === b.file_ordinal && a.root_id === b.root_id && a.path === b.path && a.old_path === b.old_path;
}

export function feedbackSourceState(anchor: { source: FeedbackSourceRef; content_digest: string },
  viewed: ResolvedFeedbackSource | null): 'current' | 'historical' | 'unavailable' {
  if (!viewed) return 'unavailable';
  return sameFeedbackSource(anchor.source, viewed.source) && anchor.content_digest === viewed.content_digest ? 'current' : 'historical';
}

export interface TargetSummaryInput {
  locator: FeedbackLocator; source: FeedbackSourceRef | null; sourceTitle: string | null;
}
export interface TargetSummary { label: string; quote: string | null; quoteKind: 'canonical' | 'observed' | null }

/**
 * Presentation-only reading of a raw Markdown quote. Chat anchors keep exact raw Markdown
 * offsets; readers see the words they selected, without emphasis/code/link syntax.
 */
export function markdownQuoteText(raw: string): string {
  return raw
    .replace(/!?\[([^\]]*)\]\([^)]*\)/g, '$1')
    .replace(/^[ \t]{0,3}(?:#{1,6}[ \t]+|>[ \t]?|[-*+][ \t]+|\d+[.)][ \t]+)/gm, '')
    .replace(/\*\*|__|~~|`+/g, '')
    .replace(/(^|[\s(])[*_](?=\S)/g, '$1')
    .replace(/(\S)[*_](?=$|[\s).,;:!?])/g, '$1')
    .replace(/\s*\n\s*/g, ' ')
    .trim();
}

/** Only locator evidence supplies quotations. Element targets deliberately use scope fallbacks. */
export function targetSummary({ locator, source, sourceTitle }: TargetSummaryInput): TargetSummary {
  const display = (quote: string | null) => quote && source?.kind === 'transcript' ? markdownQuoteText(quote) : quote;
  const result = (label: string, raw: string | null = null, quoteKind: TargetSummary['quoteKind'] = null): TargetSummary => {
    const quote = display(raw);
    return { label, quote: quote?.trim() ? quote : null, quoteKind: quote?.trim() ? quoteKind : null };
  };
  switch (locator.kind) {
    // The surrounding panel names the document; repeating its title here only adds noise.
    case 'whole': return result(`Whole ${source?.kind === 'transcript' ? 'message' : source?.kind === 'document' || sourceTitle?.trim() ? 'document' : 'source'}`);
    case 'text': return result('Selected text', locator.range.exact, 'canonical');
    case 'rich': {
      if (!locator.table_range) return result(locator.field_path.length ? 'Selected field' : 'Selected block', locator.range?.exact ?? null, 'canonical');
      const range = locator.table_range, rows = range.rows;
      const continuous = rows.every((row, index) => !index || row === rows[index - 1] + 1);
      const rowLabel = continuous ? `${rows[0] + 1}${rows.length > 1 ? `–${rows.at(-1)! + 1}` : ''}` : rows.map(row => row + 1).join(', ');
      const column = (index: number): string => index < 26 ? String.fromCharCode(65 + index) : column(Math.floor(index / 26) - 1) + column(index % 26);
      const columns = range.column_end - range.column_start > 1 ? `Columns ${column(range.column_start)}–${column(range.column_end - 1)}` : `Column ${column(range.column_start)}`;
      return result(`${rows.length > 1 ? 'Rows' : 'Row'} ${rowLabel} · ${columns}`);
    }
    case 'html': return result(locator.range ? 'Observed text' : locator.shapes.length ? 'Marked area' : 'Selected element', locator.range?.exact ?? null, 'observed');
    case 'pdf': return result(`Page ${locator.page}${locator.range ? '' : ' · Marked area'}`, locator.range?.exact ?? null, 'canonical');
    case 'image': return result('Marked area');
    case 'diff': {
      const full = source?.kind === 'work_diff' ? locator.side === 'old' ? source.old_path ?? source.path : source.path : 'Selected file';
      // Executor roots are noise in a one-line label; the file and its folder identify it.
      const path = full.split('/').filter(Boolean).slice(-2).join('/') || full;
      return result(locator.scope === 'file' ? `Entire file · ${path}`
        : `${path} · ${locator.side === 'old' ? 'Old' : 'New'} lines ${locator.start_line}${locator.end_line !== locator.start_line ? `–${locator.end_line}` : ''}`);
    }
  }
}
