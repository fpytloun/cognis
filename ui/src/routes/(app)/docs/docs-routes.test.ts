import { beforeEach, describe, expect, it, vi } from 'vitest';
import { docsOverview, getDocHref, getDocsByCategory, getEmbeddedDoc, getRelatedDocs } from '$lib/docs';

describe('docs routes', () => {
  beforeEach(() => {
    globalThis.fetch = vi.fn();
  });

  it('builds docs hub data without runtime fetches', () => {
    const groups = getDocsByCategory();

    expect(docsOverview.title).toBe('Documentation');
    expect(groups.length).toBeGreaterThan(0);
    expect(groups.some((group) => group.docs.some((doc) => getDocHref(doc.slug) === '/docs/getting-started'))).toBe(true);
    expect(globalThis.fetch).not.toHaveBeenCalled();
  });

  it('resolves a known doc and its related guides without runtime fetches', () => {
    const doc = getEmbeddedDoc('getting-started');

    expect(doc?.title).toBe('Getting Started');
    expect(doc?.content).toContain('This guide walks through the shortest path to a working Cognis system');
    expect(getRelatedDocs(doc!)).not.toHaveLength(0);
    expect(globalThis.fetch).not.toHaveBeenCalled();
  });

  it('returns null for unknown slugs so the route can show a not-found state', () => {
    expect(getEmbeddedDoc('missing-doc')).toBeNull();
  });
});
