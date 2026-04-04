import { describe, expect, it } from 'vitest';

import {
  DOC_CATEGORIES,
  docsOverview,
  embeddedDocs,
  extractMarkdownTitle,
  getEmbeddedDoc,
  getDocHref,
  getOnboardingDocs,
  validateEmbeddedDocs
} from '$lib/docs';

describe('embedded docs registry', () => {
  it('contains only fixed categories and unique slugs/source paths', () => {
    const slugs = new Set<string>();
    const sourcePaths = new Set<string>();

    for (const doc of embeddedDocs) {
      expect(DOC_CATEGORIES).toContain(doc.category);
      expect(slugs.has(doc.slug)).toBe(false);
      expect(sourcePaths.has(doc.sourcePath)).toBe(false);
      slugs.add(doc.slug);
      sourcePaths.add(doc.sourcePath);
    }
  });

  it('does not expose specs docs and keeps non-empty content', () => {
    expect(docsOverview.sourcePath).toBe('docs/README.md');
    expect(docsOverview.content.trim().length).toBeGreaterThan(0);

    for (const doc of embeddedDocs) {
      expect(doc.sourcePath).not.toContain('docs/specs/');
      expect(doc.content.trim().length).toBeGreaterThan(0);
    }
  });

  it('matches markdown H1 titles to registry titles', () => {
    for (const doc of embeddedDocs) {
      expect(extractMarkdownTitle(doc.content)).toBe(doc.title);
    }
  });

  it('resolves known slugs and validates link policy', () => {
    expect(getEmbeddedDoc('getting-started')?.title).toBe('Getting Started');
    expect(validateEmbeddedDocs()).toEqual([]);
  });

  it('builds internal onboarding doc links only', () => {
    const docs = getOnboardingDocs();

    expect(docs.length).toBeGreaterThan(0);
    expect(docs.every((doc) => getDocHref(doc.slug).startsWith('/docs/'))).toBe(true);
    expect(docs.some((doc) => getDocHref(doc.slug).includes('github.com'))).toBe(false);
  });
});
