import { describe, expect, it } from 'vitest';

import {
  embeddedDocsMeta,
  getDocHref,
  getDocMeta,
  getDocsByCategoryMeta,
  getOnboardingDocsMeta,
  getRelatedDocsMeta
} from './docs-registry';

describe('docs-registry (metadata only)', () => {
  it('exposes a non-empty list of docs', () => {
    expect(embeddedDocsMeta.length).toBeGreaterThan(0);
  });

  it('every onboarding slug resolves to a known doc', () => {
    const docs = getOnboardingDocsMeta();
    expect(docs.length).toBe(embeddedDocsMeta.length);
    for (const doc of docs) {
      expect(doc.slug).toBeDefined();
      expect(doc.title).toBeDefined();
    }
  });

  it('getDocHref builds a /docs/:slug path', () => {
    expect(getDocHref('getting-started')).toBe('/docs/getting-started');
  });

  it('getDocMeta returns null for an unknown slug', () => {
    expect(getDocMeta('no-such-doc')).toBeNull();
  });

  it('getRelatedDocsMeta resolves related slugs to metas', () => {
    const gs = getDocMeta('getting-started');
    expect(gs).not.toBeNull();
    const related = getRelatedDocsMeta(gs!);
    expect(related.length).toBeGreaterThan(0);
    for (const r of related) {
      expect(getDocMeta(r.slug)).not.toBeNull();
    }
  });

  it('getDocsByCategoryMeta groups under known categories', () => {
    const groups = getDocsByCategoryMeta();
    expect(groups.length).toBeGreaterThan(0);
    for (const group of groups) {
      expect(group.docs.length).toBeGreaterThan(0);
    }
  });
});
