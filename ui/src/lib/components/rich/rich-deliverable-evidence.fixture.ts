import type { RichDeliverablePayload } from '$lib/rich-deliverable';

export const richDeliverableEvidenceFixture: RichDeliverablePayload = {
  metadata: {
    eyebrow: 'Evidence brief',
    subtitle: 'Interactive research, evidence, and decision blocks.',
    badges: ['Citations', 'Evidence', 'Decision matrix'],
  },
  sources: [
    {
      id: 's1',
      title: 'Renderer-owned interactivity notes',
      url: 'https://cognis.local/docs/rich-renderer',
      publisher: 'Cognis',
      date: '2026-07-09',
      snippet: 'Interactions are declarative and owned by the renderer, with no arbitrary scripts in payloads.',
    },
    {
      id: 's2',
      title: 'Unsafe source should not link',
      url: 'javascript:alert(1)',
      publisher: 'Untrusted',
      snippet: 'This source is intentionally unsafe and must not produce an href.',
    },
    {
      id: 's3',
      title: 'Decision matrix notes',
      url: 'https://cognis.local/docs/decision-matrix',
      publisher: 'Cognis',
      date: '2026-07-08',
      snippet: 'Sortable matrices help compare options without changing the payload model.',
    },
  ],
  blocks: [
    {
      type: 'research_answer',
      title: 'Answer with inline citations',
      description: 'A Perplexity-style answer layer with citation chips and a source rail.',
      paragraphs: [
        {
          text: 'Use declarative rich blocks for research deliverables so answers can show source metadata without embedding executable behavior.',
          citations: ['s1'],
        },
        {
          text: 'Unsafe URLs are retained as metadata but omitted from link targets by the renderer.',
          citations: ['s2'],
        },
      ],
      key_points: ['Inline citation chips open source details.', 'The source rail remains visible in full-view layouts.'],
    },
    {
      type: 'evidence_report',
      title: 'Evidence quality',
      description: 'Claim cards with confidence and expandable snippets.',
      claims: [
        {
          label: 'Claim',
          title: 'Renderer-owned interaction is sufficient for evidence UX',
          confidence: 0.86,
          content: 'The necessary interactions are disclosure, sorting, and popovers.',
          sources: ['s1'],
          evidence: [
            {
              title: 'Architecture boundary',
              text: 'Payloads remain renderer-neutral while the UI owns interaction state.',
            },
          ],
        },
        {
          label: 'Claim',
          title: 'Comparison tables benefit from sorting',
          confidence: 'medium',
          sources: ['s3'],
          evidence: [{ quote: 'Sorting is a renderer-local view concern.' }],
        },
      ],
      caveats: ['Citation snippets are summaries, not independently fetched browser pages.'],
      contradictions: ['A static PDF export would need non-interactive fallbacks.'],
    },
    {
      type: 'decision_matrix',
      title: 'Decision matrix',
      description: 'Sortable rows with a highlighted recommendation and expandable evidence.',
      columns: [
        { key: 'option', label: 'Option' },
        { key: 'score', label: 'Score', align: 'right' },
        { key: 'tradeoff', label: 'Trade-off' },
      ],
      rows: [
        {
          option: 'Renderer-owned blocks',
          score: 92,
          tradeoff: 'Small UI surface, strong safety boundary',
          recommended: true,
          evidence: [{ title: 'Best fit', text: 'Meets the interaction goals without backend or arbitrary JS changes.' }],
        },
        {
          option: 'Micro-app payloads',
          score: 38,
          tradeoff: 'High flexibility, weak safety boundary',
          evidence: [{ title: 'Rejected', text: 'Would let payloads define behavior instead of data.' }],
        },
        {
          option: 'Static Markdown',
          score: 54,
          tradeoff: 'Portable, but weak evidence navigation',
          evidence: [{ title: 'Fallback only', text: 'Good as a fallback, insufficient as the primary renderer.' }],
        },
      ],
    },
  ],
  assets: [],
  datasets: [],
  exports: [],
};
