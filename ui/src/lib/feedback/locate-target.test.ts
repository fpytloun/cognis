import { afterEach, describe, expect, it, vi } from 'vitest';
import { commentQuote, locateComment, revealComment, sourceElement } from './locate-target';
import type { FeedbackSourceRef } from './types';

const event = { store: 'intaris' as const, session_id: 'session', seq: 4, event_id: 'event', event_type: 'assistant_message' };
const transcript: FeedbackSourceRef = { kind: 'transcript', conversation_id: 'conversation', source_event: event, source_item_id: 'item-1', field_path: ['content'] };
const document_: FeedbackSourceRef = { kind: 'document', content_ref: 'dlv_1' };

afterEach(() => { document.body.innerHTML = ''; vi.useRealTimers(); });

describe('comment location', () => {
  it('finds rendered timeline items and deliverables by their source identity', () => {
    document.body.innerHTML = '<div data-timeline-item-id="item-1">Message</div><div data-deliverable-id="dlv_1">Doc</div>';
    expect(sourceElement(transcript)?.textContent).toBe('Message');
    expect(sourceElement(document_)?.textContent).toBe('Doc');
  });
  it('reads quotes from text-bearing locators and strips chat Markdown syntax', () => {
    const range = { start: 0, end: 10, exact: '**bold** move', prefix: '', suffix: '', representation: 'feedback-text-v1' as const, text_digest: 'd' };
    expect(commentQuote({ source: transcript, locator: { kind: 'text', range } })).toBe('bold move');
    expect(commentQuote({ source: document_, locator: { kind: 'whole' } })).toBeNull();
  });
  it('scrolls to the source and marks it, and reports sources that are not rendered', () => {
    vi.useFakeTimers();
    document.body.innerHTML = '<div data-deliverable-id="dlv_1"><p>Some commented paragraph</p></div>';
    const element = sourceElement(document_)!;
    element.scrollIntoView = vi.fn();
    expect(locateComment({ source: document_, locator: { kind: 'whole' } })).toBe(true);
    expect(element.scrollIntoView).toHaveBeenCalled();
    expect(element.hasAttribute('data-feedback-located')).toBe(true);
    vi.advanceTimersByTime(3000);
    expect(element.hasAttribute('data-feedback-located')).toBe(false);
    expect(locateComment({ source: transcript, locator: { kind: 'whole' } })).toBe(false);
  });
});

describe('revealing comment sources', () => {
  it('asks the conversation to load an unrendered source, then locates it', async () => {
    const reveal = vi.fn(async () => {
      document.body.innerHTML = '<div data-timeline-item-id="item-1"><p>Older message</p></div>';
      (document.querySelector('[data-timeline-item-id]') as HTMLElement).scrollIntoView = vi.fn();
      return true;
    });
    await expect(revealComment({ source: transcript, locator: { kind: 'whole' } }, reveal)).resolves.toBe(true);
    expect(reveal).toHaveBeenCalledWith(transcript);
    expect(document.querySelector('[data-feedback-located]')).not.toBeNull();
  });
  it('reports a source the conversation cannot bring back', async () => {
    await expect(revealComment({ source: transcript, locator: { kind: 'whole' } }, async () => false)).resolves.toBe(false);
    await expect(revealComment({ source: transcript, locator: { kind: 'whole' } })).resolves.toBe(false);
  });
});
