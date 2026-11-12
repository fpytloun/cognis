import type { FeedbackLocator, FeedbackSourceRef } from './types';
import { markdownQuoteText } from './target-summary';
import { looseQuoteRange, domTextRange } from './annotations/text';

/**
 * Presentation-only location of a commented target in the rendered page. It scrolls to the
 * source and paints CSS highlights; it never reads authority from the DOM or changes anchors.
 */
export interface LocatableComment { source: FeedbackSourceRef; locator: FeedbackLocator }

const PERSISTENT = 'feedback-comment';
const FOCUSED = 'feedback-located';
let clearFocused = 0;

function highlights(): HighlightRegistry | null {
  return typeof CSS !== 'undefined' && 'highlights' in CSS ? CSS.highlights : null;
}

/** The rendered element that holds a commented source in the conversation. */
export function sourceElement(source: FeedbackSourceRef, root: ParentNode = document): HTMLElement | null {
  const id = source.kind === 'document' ? source.content_ref : source.source_item_id;
  const escaped = CSS.escape(id);
  return root.querySelector<HTMLElement>(`[data-deliverable-id="${escaped}"], [data-timeline-item-id="${escaped}"], [data-message-id="${escaped}"]`);
}

/** The quoted text a locator points at, when it has one. */
export function commentQuote(comment: LocatableComment): string | null {
  const { locator, source } = comment;
  const raw = locator.kind === 'text' ? locator.range.exact
    : locator.kind === 'rich' || locator.kind === 'pdf' || locator.kind === 'html' ? locator.range?.exact ?? null
      : locator.kind === 'diff' ? locator.quote ?? null : null;
  if (!raw?.trim()) return null;
  return source.kind === 'transcript' ? markdownQuoteText(raw) : raw;
}

function quoteRange(element: HTMLElement, quote: string): Range | null {
  const position = looseQuoteRange(element.textContent ?? '', quote, '');
  return position ? domTextRange(element, position.start, position.end) : null;
}

function rangesFor(comment: LocatableComment, root: ParentNode): Range[] {
  const element = sourceElement(comment.source, root);
  const quote = commentQuote(comment);
  if (!element || !quote) return [];
  const range = quoteRange(element, quote);
  return range ? [range] : [];
}

/** Keep every visible commented quote highlighted while comments are being reviewed. */
export function highlightComments(comments: LocatableComment[], root: ParentNode = document): () => void {
  const registry = highlights();
  if (!registry) return () => {};
  const ranges = comments.flatMap(comment => rangesFor(comment, root));
  registry.set(PERSISTENT, new Highlight(...ranges));
  return () => { if (registry.get(PERSISTENT)) registry.delete(PERSISTENT); };
}

/**
 * Scroll to a comment's target and emphasise it. Returns false when its source is not rendered
 * (for example, older history that is not loaded).
 */
export function locateComment(comment: LocatableComment, root: ParentNode = document): boolean {
  const element = sourceElement(comment.source, root);
  if (!element) return false;
  const quote = commentQuote(comment);
  const range = quote ? quoteRange(element, quote) : null;
  const anchor = range?.startContainer.parentElement ?? element;
  anchor.scrollIntoView({ block: 'center', behavior: 'smooth' });
  const registry = highlights();
  window.clearTimeout(clearFocused);
  if (range && registry) {
    registry.set(FOCUSED, new Highlight(range));
    clearFocused = window.setTimeout(() => registry.delete(FOCUSED), 4000);
  } else {
    element.setAttribute('data-feedback-located', '');
    clearFocused = window.setTimeout(() => element.removeAttribute('data-feedback-located'), 2500);
  }
  return true;
}

/**
 * Locate a comment, asking the conversation view to load and render its source first when it
 * is outside the rendered window or older than the loaded history.
 */
export async function revealComment(
  comment: LocatableComment, reveal?: (source: FeedbackSourceRef) => Promise<boolean>, root: ParentNode = document,
): Promise<boolean> {
  if (locateComment(comment, root)) return true;
  if (!reveal || !await reveal(comment.source)) return false;
  // Rendering the revealed window can take a frame or two.
  for (let attempt = 0; attempt < 10; attempt++) {
    await new Promise(resolve => requestAnimationFrame(() => resolve(undefined)));
    if (locateComment(comment, root)) return true;
  }
  return false;
}
