import { describe, expect, it } from 'vitest';

import {
  buildCitationRegistry,
  buildTocItems,
  citationNumber,
  decorateBlocks,
  normalizeDoi,
  nestTocItems,
  orderedSources,
  publicationOptions,
  sourceDetails,
} from './publication';

describe('rich publication helpers', () => {
  const blocks = [
    { type: 'section', title: 'Overview', children: [{ type: 'section', title: 'Detail' }] },
    { type: 'section', title: 'Overview' },
    { type: 'section', title: 'Results' },
    { type: 'section', title: 'Appendix' },
  ];

  it('applies automatic and explicit TOC policy with deterministic duplicate IDs', () => {
    expect(publicationOptions({}, blocks).showToc).toBe(false);
    expect(publicationOptions({ toc: false }, blocks).showToc).toBe(false);
    expect(publicationOptions({ toc: true }, blocks.slice(0, 1)).showToc).toBe(true);
    const substantial = Array.from({ length: 10 }, (_, index) => ({
      type: 'markdown',
      title: `Chapter ${index + 1}`,
      content: `Detailed evidence ${index + 1}. `.repeat(24),
    }));
    expect(publicationOptions({}, substantial).showToc).toBe(true);

    const items = buildTocItems(blocks, 3);
    expect(items.map((item) => [item.anchor, item.level])).toEqual([
      ['overview', 2],
      ['detail', 3],
      ['overview-2', 2],
      ['results', 2],
      ['appendix', 2],
    ]);
  });

  it('keeps short reports light and decorates publication numbering only when enabled', () => {
    expect(publicationOptions({}, blocks.slice(0, 2)).showToc).toBe(false);
    const content = [
      { type: 'figure', caption: 'Architecture' },
      { type: 'table', caption: 'Results' },
    ];
    const options = publicationOptions({ publication: true }, content);
    const decorated = decorateBlocks(content, buildTocItems(content), options);
    expect(decorated[0].__figure_number).toBe(1);
    expect(decorated[1].__table_number).toBe(1);
  });

  it('makes pulse presentation authoritative over TOC and publication numbering', () => {
    const content = [
      ...Array.from({ length: 6 }, (_, index) => ({
        type: 'section',
        title: `News ${index}`,
        content: 'Substantial newsroom copy. '.repeat(50),
      })),
      { type: 'figure', caption: 'Lead image' },
      { type: 'table', caption: 'Market table' },
    ];
    const options = publicationOptions({
      presentation: 'pulse',
      toc: true,
      publication: true,
      number_figures: true,
      number_tables: true,
    }, content);
    const decorated = decorateBlocks(content, buildTocItems(content), options);

    expect(options).toEqual({ showToc: false, tocDepth: 2, numberFigures: false, numberTables: false });
    expect(decorated.at(-2)?.__figure_number).toBeUndefined();
    expect(decorated.at(-1)?.__table_number).toBeUndefined();
  });

  it('uses the shared depth-only TOC, markdown-heading, and granular numbering contract', () => {
    const content = [
      { type: 'markdown', content: '## Markdown overview\n\nBody' },
      { type: 'section', title: 'Parent', children: [{ type: 'section', title: 'Nested' }] },
      { type: 'figure', caption: 'Not numbered' },
      { type: 'table', caption: 'Numbered' },
    ];
    const options = publicationOptions({
      toc: { depth: 3 },
      show_toc: true,
      publication: true,
      number_figures: false,
      number_tables: true,
    }, content);
    const items = buildTocItems(content, options.tocDepth);
    const decorated = decorateBlocks(content, items, options);

    expect(options).toEqual({ showToc: true, tocDepth: 3, numberFigures: false, numberTables: true });
    expect(items.map((item) => [item.anchor, item.level])).toEqual([
      ['markdown-overview', 2],
      ['parent', 2],
      ['nested', 3],
    ]);
    expect(decorated[0].__legacy_anchor).toBe('rich-section-0');
    expect(decorated[2].__figure_number).toBeUndefined();
    expect(decorated[3].__table_number).toBe(1);
  });

  it('omits the document hero and preserves skipped H2/H4 hierarchy with stable IDs', () => {
    const content = [
      { type: 'hero', title: 'Document title' },
      {
        type: 'markdown',
        content: '# Overview\n\nBody\n\n### Edge cases\n\nMore',
      },
      {
        type: 'section',
        title: 'Implementation',
        children: [{
          type: 'section',
          title: 'Validation',
          children: [{ type: 'section', title: 'Mobile' }],
        }],
      },
      { type: 'section', title: 'Overview' },
    ];

    const items = buildTocItems(content, 4);
    expect(items.map(({ anchor, label, level }) => [anchor, label, level])).toEqual([
      ['overview', 'Overview', 2],
      ['edge-cases', 'Edge cases', 4],
      ['implementation', 'Implementation', 2],
      ['validation', 'Validation', 3],
      ['mobile', 'Mobile', 4],
      ['overview-2', 'Overview', 2],
    ]);
    const nested = nestTocItems(items);
    expect(nested[0].children.map((node) => node.item.label)).toEqual(['Edge cases']);
    expect(nested[1].children[0].children[0].item.label).toBe('Mobile');
  });

  it('deduplicates sources by stable identity, sanitizes URLs, and formats graceful metadata', () => {
    const sources = orderedSources([
      { id: 'paper', authors: ['A. One', 'B. Two'], title: 'Paper', publication: 'Journal', year: 2025, doi: '10.1/paper' },
      { id: 'paper', title: 'Duplicate' },
      { title: 'Unsafe', url: 'javascript:alert(1)' },
    ]);
    expect(sources).toHaveLength(2);
    expect(sources[1].url).toBe('');
    expect(sourceDetails(sources[0])).toContain('A. One, B. Two · Journal · 2025 · doi: 10.1/paper');
  });

  it('excludes item-backed entries while retaining explicit canonical child blocks', () => {
    const content = [
      { type: 'tabs', title: 'Tabbed analysis', items: [{ type: 'section', title: 'Synthetic tab item' }] },
      { type: 'accordion', title: 'Questions', items: [{ type: 'section', title: 'Collapsed answer' }] },
      { type: 'gallery', title: 'Figures', items: [{ type: 'figure', title: 'Gallery image' }] },
      {
        type: 'section',
        title: 'Canonical parent',
        children: [{ type: 'section', title: 'Canonical child' }],
      },
    ];

    expect(buildTocItems(content, 3).map((item) => item.label)).toEqual([
      'Tabbed analysis',
      'Questions',
      'Figures',
      'Canonical parent',
      'Canonical child',
    ]);
  });

  it('numbers citations globally by first use and deduplicates aliases and repeated groups', () => {
    const sources = [
      { id: 'a', title: 'A' },
      { citation_id: 'b', title: 'B' },
    ];
    const content = [
      { type: 'research_answer', paragraphs: [{ text: 'First', citations: ['b', 'b', 'a'] }] },
      {
        type: 'research_answer',
        paragraphs: [
          { text: 'Second', source_ids: 'a' },
          { text: 'Inline', sources: { id: 'c', title: 'C' } },
        ],
      },
    ];
    const registry = buildCitationRegistry(content, sources);

    expect(registry.sources.map((source) => source.title)).toEqual(['B', 'A', 'C']);
    expect(citationNumber(registry, registry.sources[0])).toBe(1);
    expect(citationNumber(registry, registry.sources[1])).toBe(2);
    expect(citationNumber(registry, registry.sources[2])).toBe(3);
  });

  it('registers direct-answer and matrix-row citations in document order', () => {
    const registry = buildCitationRegistry(
      [
        { type: 'research_answer', paragraphs: [{ text: 'First', source_ids: ['paragraph'] }] },
        { type: 'research_answer', answer: 'Second', source_ids: ['answer'] },
        { type: 'comparison_matrix', rows: [{ name: 'Product', source_ids: ['matrix'] }] },
      ],
      [
        { id: 'paragraph', title: 'Paragraph source' },
        { id: 'answer', title: 'Answer source' },
        { id: 'matrix', title: 'Matrix source' },
      ],
    );

    expect(registry.sources.map((source) => source.title)).toEqual([
      'Paragraph source',
      'Answer source',
      'Matrix source',
    ]);
  });

  it('does not register source scopes as direct-answer citations', () => {
    const registry = buildCitationRegistry(
      [{
        type: 'research_answer',
        sources: ['scope-only', 'cited'],
        paragraphs: [{ text: 'Claim', source_ids: ['cited'] }],
      }],
      [
        { id: 'scope-only', title: 'Scope only' },
        { id: 'cited', title: 'Cited source' },
      ],
    );

    expect(registry.sources.map((source) => source.title)).toEqual(['Cited source']);
  });

  it('normalizes equivalent DOI prefixes case-insensitively', () => {
    expect(normalizeDoi('HTTPS://DOI.ORG/10.1000/Test')).toBe('10.1000/Test');
    expect(normalizeDoi('DoI: 10.1000/Test')).toBe('10.1000/Test');
    expect(orderedSources([
      { doi: 'HTTPS://DOI.ORG/10.1000/Test', title: 'A' },
      { doi: 'doi:10.1000/test', title: 'B' },
    ])).toHaveLength(1);
  });

  it('uses locale-independent Unicode normalization for anchors and identities', () => {
    expect(buildTocItems([
      { type: 'section', title: 'İSTANBUL Résumé' },
      { type: 'section', title: 'istanbul resume' },
    ]).map((item) => item.anchor)).toEqual(['istanbul-resume', 'istanbul-resume-2']);
    expect(orderedSources([
      { id: 'İD', title: 'First' },
      { id: 'i̇d', title: 'Second' },
    ])).toHaveLength(1);
  });

  it('deduplicates inline citation groups by normalized DOI identity', () => {
    const registry = buildCitationRegistry([{
      type: 'research_answer',
      paragraphs: [{
        text: 'Claim',
        citations: [
          { title: 'First alias', doi: 'HTTPS://DOI.ORG/10.1000/Test', url: 'https://first.example' },
          { title: 'Second alias', doi: 'doi:10.1000/test', href: 'https://second.example' },
        ],
      }],
    }], []);

    expect(registry.sources).toHaveLength(1);
    expect(registry.sources[0].title).toBe('First alias');
  });
});
