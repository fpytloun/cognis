import { describe, expect, it } from 'vitest';

import {
  DOC_CATEGORIES,
  extractMarkdownTitle,
  getEmbeddedDoc,
  getDocHref,
  getOnboardingDocs,
  loadDocsOverviewContent,
  loadEmbeddedDocs,
  validateEmbeddedDocsContent
} from '$lib/docs';

describe('embedded docs registry', () => {
  it('contains only fixed categories and unique slugs/source paths', async () => {
    const embeddedDocs = await loadEmbeddedDocs();
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

  it('does not expose specs docs and keeps non-empty content', async () => {
    const docsOverview = await loadDocsOverviewContent();
    const embeddedDocs = await loadEmbeddedDocs();
    expect(docsOverview.sourcePath).toBe('docs/README.md');
    expect(docsOverview.content.trim().length).toBeGreaterThan(0);

    for (const doc of embeddedDocs) {
      expect(doc.sourcePath).not.toContain('docs/specs/');
      expect(doc.content.trim().length).toBeGreaterThan(0);
    }
  });

  it('matches markdown H1 titles to registry titles', async () => {
    const embeddedDocs = await loadEmbeddedDocs();
    for (const doc of embeddedDocs) {
      expect(extractMarkdownTitle(doc.content)).toBe(doc.title);
    }
  });

  it('resolves known slugs and validates link policy', async () => {
    expect((await getEmbeddedDoc('getting-started'))?.title).toBe('Getting Started');
    expect((await getEmbeddedDoc('architecture'))?.title).toBe('Architecture');
    expect((await getEmbeddedDoc('settings'))?.title).toBe('Settings');
    expect((await getEmbeddedDoc('tools-and-skills'))?.title).toBe('Tools and Skills');
    expect(await validateEmbeddedDocsContent()).toEqual([]);
  });

  it('rewrites internal relative doc links to supported embedded or repo targets', async () => {
    const docsOverview = await loadDocsOverviewContent();
    expect(docsOverview.content).toContain('/blob/main/docs/specs/README.md');
    expect(docsOverview.content).not.toContain('](specs/README.md)');
  });

  it('builds internal onboarding doc links only', async () => {
    const docs = await getOnboardingDocs();

    expect(docs.length).toBeGreaterThan(0);
    expect(docs.every((doc) => getDocHref(doc.slug).startsWith('/docs/'))).toBe(true);
    expect(docs.some((doc) => getDocHref(doc.slug).includes('github.com'))).toBe(false);
  });

  it('rewrites allowlisted relative svg assets into bundled URLs', async () => {
    const architecture = await getEmbeddedDoc('architecture');

    expect(architecture?.content).toContain('cognis-ecosystem-overview');
    expect(architecture?.content).not.toContain('../assets/images/');
  });
});
