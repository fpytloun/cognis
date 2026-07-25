import type { RichDeliverablePayload } from '$lib/rich-deliverable';

export interface RichDeliverableVisualScenario {
  id: string;
  title: string;
  description: string;
  content: string;
  payload: RichDeliverablePayload;
}

const commonSources = [
  {
    title: 'OpenAI — Structured Outputs',
    url: 'https://platform.openai.com/docs/guides/structured-outputs',
    publisher: 'OpenAI Docs',
    date: '2026-07-02',
  },
  {
    title: 'Anthropic — Tool use and artifacts',
    url: 'https://docs.anthropic.com/',
    publisher: 'Anthropic Docs',
    date: '2026-06-28',
  },
  {
    title: 'Cognis internal telemetry sample',
    url: 'https://cognis.local/reports/rich-deliverables',
    publisher: 'Cognis',
    date: '2026-07-09',
  },
];

const catNapFigure = '/docs/rich-deliverables/cat-nap.jpg';
const catGalleryWindow = '/docs/rich-deliverables/cat-window.jpg';
const catGalleryBox = '/docs/rich-deliverables/cat-box.jpg';

export const richDeliverableFallbackContent =
  '# Rich deliverable fallback\n\nThis Markdown fallback is available when rich rendering is unavailable.';

export const richDeliverableVisualScenarios: RichDeliverableVisualScenario[] = [
  {
    id: 'research-answer',
    title: 'Perplexity-style research answer',
    description: 'A source-backed answer with direct recommendation, evidence cards, comparison table, and citations.',
    content: 'A source-backed AI market answer with evidence, sources, and a concise recommendation.',
    payload: {
      metadata: {
        eyebrow: 'Research brief',
        subtitle: 'A polished answer format for source-heavy research and synthesis tasks.',
        badges: ['Source linked', 'Decision ready', '5 min read'],
      },
      blocks: [
        {
          type: 'hero',
          eyebrow: 'Answer',
          title: 'Managed, policy-driven agent tools beat naïve MCP for production workflows',
          subtitle:
            'The durable pattern is not “give the model every tool.” It is deferred access, scoped policies, audit trails, and app-owned UX for high-value outputs.',
          badges: ['Recommendation', 'Architecture', 'Risk-aware'],
        },
        {
          type: 'grid',
          blocks: [
            { type: 'metric', label: 'Confidence', value: 'High', delta: '4 independent signals', tone: 'success' },
            { type: 'metric', label: 'Implementation risk', value: 'Medium', delta: 'Policy surface', tone: 'warning' },
            { type: 'metric', label: 'Time to MVP', value: '2–3 weeks', delta: 'Existing Cognis primitives', tone: 'neutral' },
          ],
        },
        {
          type: 'callout',
          tone: 'success',
          title: 'Bottom line',
          content:
            'Ship rich deliverables as **structured product surfaces**, not prettier Markdown. The data model should stay renderer-neutral, while the UI owns layout, hierarchy, and interactions.',
        },
        {
          type: 'markdown',
          content: [
            '## Markdown fallback typography',
            '',
            'Rich markdown blocks should remain as readable as normal assistant messages, including headings, emphasis, inline `code`, and forgiving table rendering.',
            '',
            '| Phase | Work | Branch/worktree | Parallel? |',
            '|---|---|--|',
            '| A | WS1 + WS2 (same files, one coherent change set) | fix/projection-critical-pressure | no — foundation |',
            '| B1 | WS3 (prefix stability) | feat/projection-prefix-stability on top of A | parallel with B2 |',
            '| B2 | WS4 (ingestion caps) | feat/presure-aware-ingestion on top of A | parallel with B1 |',
            '| C | WS5 synthetic long-turn suite + integration, merge B1/B2, full validation | integration worktree | after B |',
          ].join('\n'),
        },
        {
          type: 'card_grid',
          blocks: [
            {
              type: 'card',
              eyebrow: 'Why it works',
              title: 'Structure survives replay',
              content:
                'The renderer can hydrate canonical content by deliverable ID, keeping refresh and raw/debug views stable even after tool outputs are compacted.',
            },
            {
              type: 'card',
              eyebrow: 'Why Markdown loses',
              title: 'No semantic layout',
              content:
                'A Markdown answer can imitate tables and headings, but cannot express metrics, source cards, charts, timelines, or full-view affordances reliably.',
            },
            {
              type: 'card',
              eyebrow: 'Product implication',
              title: 'Design for outcomes',
              content:
                'Research, incident reports, dashboards, and implementation plans should each have first-class blocks matching how users scan those artifacts.',
            },
          ],
        },
        {
          type: 'comparison_matrix',
          title: 'Format comparison',
          columns: [
            { key: 'dimension', label: 'Dimension' },
            { key: 'markdown', label: 'Markdown' },
            { key: 'rich', label: 'Rich deliverable' },
          ],
          rows: [
            { dimension: 'Decision summary', markdown: 'Headings and bullets', rich: 'Hero, callout, ranked cards' },
            { dimension: 'Evidence', markdown: 'Footnotes or links', rich: 'Source cards and citations' },
            { dimension: 'Status', markdown: 'Plain text', rich: 'Metrics, deltas, timelines' },
            { dimension: 'Replay durability', markdown: 'Transcript text', rich: 'Canonical payload by deliverable ID' },
          ],
        },
        {
          type: 'source_list',
          title: 'Sources used',
        },
      ],
      sources: commonSources,
      assets: [],
      datasets: [],
      exports: [],
    },
  },
  {
    id: 'incident-report',
    title: 'Production incident report',
    description: 'Operational report with severity, timeline, impact, metrics, root cause, and corrective actions.',
    content: 'Incident report fallback content.',
    payload: {
      metadata: {
        eyebrow: 'Incident report',
        subtitle: 'Operationally useful, skim-friendly, and audit-friendly.',
        badges: ['P1', 'Resolved', 'SLO impact'],
      },
      blocks: [
        {
          type: 'hero',
          eyebrow: 'P1 resolved',
          title: 'Chat turn failures during rich deliverable replay',
          subtitle: 'Root cause was an app-specific event type emitted into a generic Intaris append-only event stream.',
          badges: ['31 min duration', 'No data loss', 'Hotfix shipped'],
        },
        {
          type: 'kv',
          title: 'Incident facts',
          items: [
            { label: 'Started', value: '10:42 UTC' },
            { label: 'Detected by', value: 'Direct smoke test' },
            { label: 'Customer impact', value: 'Rich deliverable writes failed post-tool' },
            { label: 'Resolution', value: 'Lifecycle subtype event' },
          ],
        },
        {
          type: 'timeline',
          title: 'Timeline',
          items: [
            { time: '10:42', title: 'Validation error', content: 'SessionEvent was constructed with event_kind instead of type.', tone: 'danger' },
            { time: '10:51', title: 'HTTPStatusError', content: 'Intaris rejected the app-specific event type.', tone: 'warning' },
            { time: '11:04', title: 'Boundary fixed', content: 'Cognis now emits lifecycle.event=assistant_deliverable.', tone: 'success' },
            { time: '11:18', title: 'Renderer gap exposed', content: 'Persistence is fixed; UX polish and block coverage remain.', tone: 'neutral' },
          ],
        },
        {
          type: 'chart',
          title: 'Error rate during mitigation',
          description: 'Synthetic incident curve for validating chart rendering.',
          spec_version: 'cognis.chart.v1',
          chart_type: 'line',
          series: [{
            id: 'error-rate',
            label: 'Error rate',
            points: [
              { x: '10:40', y: 2 },
              { x: '10:50', y: 9 },
              { x: '11:00', y: 6 },
              { x: '11:10', y: 1 },
              { x: '11:20', y: 0 },
            ],
          }],
          x_axis: { type: 'category', label: 'Mitigation time' },
          y_axis: { type: 'linear', label: 'Error rate', unit: '%', min: 0, max: 10 },
          legend_position: 'none',
          palette_token: 'cool',
        },
        {
          type: 'table',
          title: 'Corrective actions',
          columns: [
            { key: 'action', label: 'Action' },
            { key: 'owner', label: 'Owner' },
            { key: 'status', label: 'Status' },
          ],
          rows: [
            { action: 'Use generic lifecycle event subtype', owner: 'Cognis UI/API', status: 'Done' },
            { action: 'Add event-construction regression', owner: 'Agent loop', status: 'Done' },
            { action: 'Add visual E2E fixture', owner: 'Frontend', status: 'In progress' },
          ],
        },
      ],
      sources: commonSources,
      assets: [],
      datasets: [],
      exports: [],
    },
  },
  {
    id: 'product-comparison',
    title: 'Product comparison and buying guide',
    description: 'A consumer-style comparison with recommendation cards and trade-off matrix.',
    content: 'Product comparison fallback content.',
    payload: {
      metadata: {
        eyebrow: 'Buying guide',
        subtitle: 'Useful for shopping, vendor selection, and architectural trade-offs.',
        badges: ['Recommendation', 'Trade-offs', 'Decision matrix'],
      },
      blocks: [
        {
          type: 'hero',
          eyebrow: 'Recommendation',
          title: 'Choose the managed option unless you need custom policy hooks',
          subtitle: 'The premium plan wins for reliability; the open-core option wins for extensibility.',
        },
        {
          type: 'comparison_matrix',
          title: 'Decision matrix',
          columns: [
            { key: 'criteria', label: 'Criteria' },
            { key: 'managed', label: 'Managed' },
            { key: 'open_core', label: 'Open core' },
            { key: 'verdict', label: 'Verdict' },
          ],
          rows: [
            { criteria: 'Time to value', managed: 'Same day', open_core: '1–2 weeks', verdict: 'Managed' },
            { criteria: 'Policy control', managed: 'Limited', open_core: 'Deep hooks', verdict: 'Open core' },
            { criteria: 'Ops burden', managed: 'Low', open_core: 'Medium', verdict: 'Managed' },
            { criteria: 'Cost predictability', managed: 'High', open_core: 'Depends on usage', verdict: 'Managed' },
          ],
        },
        {
          type: 'card_grid',
          blocks: [
            { type: 'card', tone: 'success', title: 'Best default', content: 'Managed plan: lower operational burden and faster rollout.' },
            { type: 'card', tone: 'warning', title: 'Best for teams with platform capacity', content: 'Open core: use when policy control is worth owning the runtime.' },
            { type: 'card', title: 'Avoid', content: 'A bespoke internal fork before the core workflows are stable.' },
          ],
        },
        {
          type: 'quote',
          content: 'The real decision is not hosted vs self-hosted; it is whether you need to own the policy surface.',
          byline: 'Cognis product note',
        },
      ],
      sources: commonSources,
      assets: [],
      datasets: [],
      exports: [],
    },
  },
  {
    id: 'metrics-dashboard',
    title: 'Weekly metrics dashboard',
    description: 'Dashboard use case with status cards, charts, and trend table.',
    content: 'Metrics dashboard fallback content.',
    payload: {
      metadata: {
        eyebrow: 'Weekly dashboard',
        subtitle: 'For status updates, analytics summaries, and executive reporting.',
        badges: ['Trend', 'KPI', 'Operational'],
      },
      blocks: [
        {
          type: 'hero',
          eyebrow: 'Week 28',
          title: 'Agent reliability improved while rich-output adoption increased',
          subtitle: 'Throughput is stable; the remaining risk is renderer maturity and visual QA coverage.',
        },
        {
          type: 'grid',
          blocks: [
            { type: 'metric', label: 'Completed turns', value: '18.4k', delta: '+12%', tone: 'success' },
            { type: 'metric', label: 'Tool failures', value: '0.7%', delta: '-0.4 pp', tone: 'success' },
            { type: 'metric', label: 'Rich outputs', value: '146', delta: '+38%', tone: 'neutral' },
            { type: 'metric', label: 'Median latency', value: '4.8s', delta: '+0.2s', tone: 'warning' },
          ],
        },
        {
          type: 'chart',
          title: 'Rich deliverable adoption',
          description: 'Synthetic weekday output mix for validating grouped canonical series.',
          spec_version: 'cognis.chart.v1',
          chart_type: 'grouped_bar',
          series: [
            {
              id: 'rich-deliverables',
              label: 'Rich deliverables',
              points: [
                { x: 'Mon', y: 14 }, { x: 'Tue', y: 21 }, { x: 'Wed', y: 27 },
                { x: 'Thu', y: 35 }, { x: 'Fri', y: 49 },
              ],
            },
            {
              id: 'markdown-only',
              label: 'Markdown-only',
              points: [
                { x: 'Mon', y: 82 }, { x: 'Tue', y: 76 }, { x: 'Wed', y: 68 },
                { x: 'Thu', y: 57 }, { x: 'Fri', y: 43 },
              ],
            },
          ],
          x_axis: { type: 'category', label: 'Weekday' },
          y_axis: { type: 'linear', label: 'Outputs', unit: 'deliverables', min: 0, max: 100 },
          stack: false,
          legend_position: 'bottom',
          palette_token: 'default',
        },
        {
          type: 'table',
          title: 'Top workflow categories',
          columns: [
            { key: 'category', label: 'Category' },
            { key: 'volume', label: 'Volume', align: 'right' },
            { key: 'success', label: 'Success rate', align: 'right' },
          ],
          rows: [
            { category: 'Research reports', volume: 58, success: '98.2%' },
            { category: 'Incident summaries', volume: 31, success: '99.1%' },
            { category: 'Implementation plans', volume: 44, success: '96.8%' },
          ],
        },
      ],
      sources: commonSources,
      assets: [],
      datasets: [],
      exports: [],
    },
  },
  {
    id: 'implementation-plan',
    title: 'Implementation plan',
    description: 'Engineering handoff with plan, sequence diagram, code/config snippet, and risks.',
    content: 'Implementation plan fallback content.',
    payload: {
      metadata: {
        eyebrow: 'Engineering plan',
        subtitle: 'Readable by humans, actionable by engineers, and compact enough for chat.',
        badges: ['Architecture', 'Tasks', 'Validation'],
      },
      blocks: [
        {
          type: 'hero',
          eyebrow: 'Plan',
          title: 'Ship rich deliverables as canonical content references',
          subtitle: 'Keep Intaris generic, keep payloads in Cognis deliverables, and let the UI own rich presentation.',
        },
        {
          type: 'steps',
          title: 'Execution sequence',
          items: [
            { step: '01', title: 'Persist payload', content: 'Write canonical rich payload to deliverables table.' },
            { step: '02', title: 'Emit lifecycle reference', content: 'Store generic lifecycle event with deliverable_id.' },
            { step: '03', title: 'Hydrate in UI', content: 'Fetch payload by ID and render rich blocks.' },
            { step: '04', title: 'Visual QA', content: 'Run Playwright fixture across scenarios and viewports.' },
          ],
        },
        {
          type: 'chart',
          title: 'Validation coverage',
          description: 'Covered validation paths in the implementation fixture.',
          spec_version: 'cognis.chart.v1',
          chart_type: 'bar',
          series: [{
            id: 'coverage',
            label: 'Coverage',
            points: [{ x: 'Covered paths', y: 3 }],
          }],
          x_axis: { type: 'category', label: 'Validation status' },
          y_axis: { type: 'linear', label: 'Paths', unit: 'paths', min: 0, max: 4 },
          legend_position: 'none',
          palette_token: 'default',
        },
        { type: 'markdown', content: 'Chart child content is visible exactly once.' },
        {
          type: 'mermaid',
          title: 'Data flow',
          source: 'flowchart LR\n  A[write_deliverable] --> B[(deliverables table)]\n  A --> C[Intaris lifecycle event]\n  C --> D[Chat timeline]\n  D --> E[UI fetch by deliverable_id]\n  E --> F[Rich renderer]',
          children: [{ type: 'markdown', content: 'Mermaid child content is visible exactly once.' }],
        },
        {
          type: 'code',
          title: 'Reference event shape',
           content:
             'SessionEvent(type="lifecycle", data={"event": "assistant_deliverable", "deliverable_id": "dlv_..."})',
         },
        {
          type: 'divider',
          children: [{ type: 'markdown', content: 'Divider child content follows the divider exactly once.' }],
        },
      ],
      sources: commonSources,
      assets: [],
      datasets: [],
      exports: [],
    },
  },
  {
    id: 'evidence-report',
    title: 'Evidence-heavy source report',
    description: 'A report that emphasizes references, links, and auditability.',
    content: 'Evidence report fallback content.',
    payload: {
      metadata: {
        eyebrow: 'Evidence report',
        subtitle: 'Designed for audit trails, source checking, and shareable research summaries.',
        badges: ['Citations', 'Audit trail', 'External links'],
      },
      blocks: [
        {
          type: 'hero',
          eyebrow: 'Evidence',
          title: 'Every high-confidence claim needs a visible source trail',
          subtitle: 'Rich deliverables should make source confidence and provenance visible without forcing users into raw/debug.',
        },
        {
          type: 'columns',
          blocks: [
            {
              type: 'link_preview',
              site: 'OpenAI Docs',
              title: 'Structured Outputs',
              url: 'https://platform.openai.com/docs/guides/structured-outputs',
              description: 'Reference for schema-constrained model outputs and tool result structures.',
            },
            {
              type: 'link_preview',
              site: 'Cognis Design Note',
              title: 'Rich Deliverables and Micro Apps',
              url: 'https://cognis.local/notes/rich-deliverables',
              description: 'Internal product direction for block-composed, renderer-neutral outputs.',
            },
          ],
        },
        {
          type: 'callout',
          tone: 'warning',
          title: 'Audit rule',
          content: 'If the output makes a load-bearing claim, the rich renderer should make the supporting source easy to inspect.',
        },
        {
          type: 'source_list',
          title: 'Evidence index',
        },
      ],
      sources: commonSources,
      assets: [],
      datasets: [],
      exports: [],
    },
  },
  {
    id: 'publication-report',
    title: 'Publication-grade technical report',
    description: 'A realistic long-form fixture covering navigation, citations, figures, tables, code, diagrams, and an appendix.',
    content: 'Publication report fallback with abstract, findings, implementation, validation, appendix, and references.',
    payload: {
      metadata: {
        eyebrow: 'Technical publication',
        subtitle: 'A realistic fixture for PDF and responsive publication QA.',
        badges: ['Peer-review shape', 'IEEE citations', 'Appendix'],
        toc: { enabled: true, depth: 3 },
        publication: true,
      },
      blocks: [
        {
          type: 'hero',
          eyebrow: 'Research report',
          title: 'Progressive Rich Deliverables for publication-grade agent output',
          subtitle: 'A renderer-neutral approach that stays lightweight for short reports while scaling to navigable technical publications.',
        },
        {
          type: 'section',
          title: 'Abstract',
          content: 'Rich Deliverables can support publication semantics without turning every response into a magazine layout. Policy-driven navigation and numbering keep simple reports restrained.',
        },
        {
          type: 'research_answer',
          title: 'Background and motivation',
          paragraphs: [
            { text: 'Semantic headings and stable anchors enable reliable navigation and PDF outlines.', citations: ['weasy'] },
            { text: 'First-use citation numbering keeps references deterministic across renderer targets.', citations: ['ieee', 'weasy'] },
          ],
        },
        {
          type: 'section',
          title: 'System design',
          content: 'The canonical payload remains block-composed. Publication metadata only activates document-level behavior.',
          blocks: [
            {
              type: 'figure',
              title: 'Renderer pipeline',
              alt: 'Three-stage renderer pipeline',
              src: 'data:image/svg+xml,%3Csvg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 640 180"%3E%3Crect width="640" height="180" fill="%23eef2f6"/%3E%3Cg fill="%23283b4c" font-family="sans-serif" font-size="22"%3E%3Ctext x="40" y="98"%3ECanonical payload%3C/text%3E%3Ctext x="265" y="98"%3ESemantics%3C/text%3E%3Ctext x="455" y="98"%3EHTML / PDF%3C/text%3E%3C/g%3E%3Cpath d="M210 90h45m145 0h45" stroke="%2352647d" stroke-width="4"/%3E%3C/svg%3E',
              caption: 'Renderer-neutral blocks are enriched with deterministic publication semantics before target rendering.',
            },
            {
              type: 'mermaid',
              title: 'Navigation flow',
              source: 'flowchart LR\n  P[Payload] --> I[Index]\n  I --> T[TOC]\n  I --> B[Bookmarks]\n  I --> R[References]',
            },
          ],
        },
        {
          type: 'section',
          title: 'Evaluation',
          content: 'The fixture exercises layout behavior using realistic captions and constrained mobile widths.',
          blocks: [
            {
              type: 'table',
              title: 'Validation matrix',
              caption: 'Required behavior across browser and PDF renderers.',
              columns: ['surface', 'navigation', 'citations', 'overflow'],
              rows: [
                { surface: 'Chat 320–430 px', navigation: 'Collapsible', citations: 'Interactive previews', overflow: 'No clipping' },
                { surface: 'Standalone HTML', navigation: 'Linked TOC', citations: 'Expandable detail', overflow: 'Responsive table' },
                { surface: 'A4 PDF', navigation: 'Leaders and pages', citations: 'IEEE bibliography', overflow: 'Repeated headers' },
              ],
            },
          ],
        },
        {
          type: 'section',
          title: 'Implementation notes',
          content: 'No Chromium runtime is added to the controller image.',
          blocks: [
            {
              type: 'code',
              title: 'Conservative metadata',
              language: 'json',
              content: '{"toc":{"enabled":true,"depth":3},"publication":{"number_figures":true,"number_tables":true}}',
            },
          ],
        },
        {
          type: 'section',
          title: 'Limitations',
          content: 'Equations remain accessible Unicode or preformatted text. Full TeX/MathML rendering is deliberately excluded until a safe lightweight dependency is justified.',
        },
         {
           type: 'section',
           title: 'Appendix A — Canonical references',
           content: 'Cross-target references use stable block `id`/`anchor` values and ordinary fragment links where authored Markdown is appropriate.',
         },
          {
            type: 'tabs',
            title: 'Supplemental views',
           items: [
             { type: 'markdown', title: 'Operational notes', content: 'Item-backed tab content remains local to the container.' },
              { type: 'markdown', title: 'Reviewer notes', content: 'It is intentionally excluded from document navigation.' },
            ],
          },
          {
            type: 'markdown',
            title: 'Closing summary',
            content: 'This titled Markdown block intentionally contains no Markdown heading.',
          },
          { type: 'source_list', title: 'Source previews' },
      ],
      sources: [
        {
          id: 'weasy',
          authors: ['S. CourtBouillon contributors'],
          title: 'WeasyPrint Features',
          publication: 'WeasyPrint Documentation',
          year: 2026,
          accessed: '2026-07-11',
          url: 'https://doc.courtbouillon.org/weasyprint/stable/api_reference.html',
          snippet: 'WeasyPrint supports PDF bookmarks, internal links, named strings, and target counters.',
        },
        {
          id: 'ieee',
          author: 'IEEE',
          title: 'IEEE Reference Guide',
          publication: 'IEEE Author Center',
          year: 2025,
          url: 'https://journals.ieeeauthorcenter.ieee.org/',
          snippet: 'IEEE references are numbered in order of first citation.',
        },
      ],
      assets: [],
      datasets: [],
      exports: [],
    },
  },
  {
    id: 'newsletter-digest',
    title: 'Weekly newsletter digest',
    description: 'A cited multi-story digest using progressive disclosure, a closing callout, and a numbered source list.',
    content: 'Weekly digest fallback covering agent tooling, model releases, and infra notes.',
    payload: {
      metadata: {
        eyebrow: 'Weekly digest',
        subtitle: 'A newsletter-style composition for scanning several short, cited stories at once.',
        badges: ['5 stories', 'Cited', 'This week'],
      },
      blocks: [
        {
          type: 'hero',
          eyebrow: 'Issue 42',
          title: 'Agent tooling consolidates around policy-scoped access',
          subtitle: 'This week: managed tool policies gain traction, two model releases, and an infra postmortem worth reading.',
        },
        {
          type: 'card_grid',
          blocks: [
            {
              type: 'card',
              variant: 'editorial',
              eyebrow: 'Agent tooling',
              title: 'Policy-scoped tool access becomes the default pattern',
              summary: 'Vendors converge on deferred, audited tool grants instead of blanket access.',
              citations: ['tool-policy'],
            },
            {
              type: 'card',
              variant: 'editorial',
              eyebrow: 'Models',
              title: 'Two mid-size model releases target agentic workloads',
              summary: 'Both emphasize long-context tool use over raw benchmark scores.',
              citations: ['model-release'],
            },
            {
              type: 'card',
              variant: 'editorial',
              eyebrow: 'Infra',
              title: 'A widely read postmortem on event-schema drift',
              summary: 'The root cause was an app-specific event shape leaking into a generic store.',
              citations: ['postmortem'],
            },
          ],
        },
        {
          type: 'accordion',
          title: 'More this week',
          items: [
            {
              type: 'card',
              variant: 'editorial',
              title: 'Editor tooling adds inline diagnostics for agent-authored code',
              summary: 'LSP-backed diagnostics now surface directly after file edits.',
              citations: ['editor-tooling'],
            },
            {
              type: 'card',
              variant: 'editorial',
              title: 'A survey of renderer-neutral document formats',
              summary: 'Block-composed payloads continue to outpace ad hoc HTML for durability.',
              citations: ['renderer-survey'],
            },
          ],
        },
        {
          type: 'callout',
          tone: 'neutral',
          title: 'Worth your time',
          content: 'If you read one thing this week, read the postmortem — schema drift between app-specific and generic event stores is an easy trap.',
        },
        {
          type: 'source_list',
          title: 'Sources',
        },
      ],
      sources: [
        { id: 'tool-policy', title: 'Policy-scoped agent tool access', url: 'https://cognis.local/notes/tool-policy', publisher: 'Cognis', date: '2026-07-06' },
        { id: 'model-release', title: 'Two new agentic model releases', url: 'https://example.org/models/agentic-release', publisher: 'Model Weekly', date: '2026-07-08' },
        { id: 'postmortem', title: 'Event-schema drift postmortem', url: 'https://cognis.local/postmortems/event-schema-drift', publisher: 'Cognis', date: '2026-07-09' },
        { id: 'editor-tooling', title: 'Inline diagnostics for agent-authored code', url: 'https://example.org/editors/inline-diagnostics', publisher: 'Editor Weekly', date: '2026-07-10' },
        { id: 'renderer-survey', title: 'A survey of renderer-neutral document formats', url: 'https://example.org/docs/renderer-survey', publisher: 'Docs Weekly', date: '2026-07-11' },
      ],
      assets: [],
      datasets: [],
      exports: [],
    },
  },
  {
    id: 'freeform-notes',
    title: 'Freeform working notes',
    description: 'A reflective, prose-heavy note where the content shapes the layout instead of forcing dashboard-style blocks.',
    content: 'Working notes fallback on renderer convergence and the cost of over-carding content.',
    payload: {
      metadata: {
        eyebrow: 'Working notes',
        subtitle: 'Prose stays prose: not every deliverable needs metrics or cards.',
      },
      blocks: [
        {
          type: 'markdown',
          title: 'On not over-carding everything',
          content: [
            'The instinct once a renderer supports cards is to put everything in a card. That is a mistake for reflective or narrative content.',
            '',
            'A working note, a design rationale, or a retrospective reads better as ordinary paragraphs with real hierarchy than as a grid of same-weight tiles. The reader benefits from continuity of thought, not from scanning fragments.',
          ].join('\n'),
        },
        {
          type: 'section',
          title: 'Where structure earns its place',
          content: 'Structure should appear only where the content actually has that shape.',
          blocks: [
            {
              type: 'timeline',
              title: 'How this view evolved',
              items: [
                { time: 'Draft 1', title: 'Dashboard-first', content: 'Everything was a metric or a status card. It looked busy and said little.' },
                { time: 'Draft 2', title: 'Narrative-first', content: 'Rewrote as prose with one callout for the actual conclusion.' },
                { time: 'Draft 3', title: 'This version', content: 'Kept the prose, added a timeline only because there really was a sequence to show.' },
              ],
            },
          ],
        },
        {
          type: 'quote',
          content: 'If a fact does not need a chart, do not give it one. If a thought does not need a card, do not give it one.',
        },
        {
          type: 'markdown',
          content: 'The rest of this note is intentionally plain: a closing reflection rather than a summary card, because a one-line status pill would flatten the point being made.',
        },
      ],
      sources: [],
      assets: [],
      datasets: [],
      exports: [],
    },
  },
  {
    id: 'visual-system-reference',
    title: 'Generic visual system reference',
    description: 'Contract-tolerant cards, media, source-linked headlines, icons, status, and editorial disclosure.',
    content: 'Visual system reference fallback.',
    payload: {
      metadata: { eyebrow: 'Reference surface', subtitle: 'Reusable generic Rich visual language.' },
      blocks: [
        {
          type: 'hero',
          eyebrow: 'General Rich',
          title: 'A responsive visual system for decision-ready deliverables',
          subtitle: 'Use semantic blocks and safe links; let the browser own media placement, visual hierarchy, and interaction.',
        },
        {
          type: 'grid',
          columns: 3,
          blocks: [
            {
              type: 'card',
              variant: 'feature',
              icon: 'activity',
              eyebrow: 'Featured analysis',
              title: 'Renderer health is stable',
              summary: 'All core visual blocks render safely across compact chat and full view.',
              href: 'https://example.org/fixture/health',
              media: {
                href: '/fixtures/daily-pulse-lovosice.svg',
                alt: 'Abstract blue editorial illustration',
                credit: 'Cognis fixture',
                placement: 'top',
                aspect_ratio: '16 / 7',
                focal_point: '50% 50%',
              },
              source_ids: ['visual-reference'],
            },
            {
              type: 'card',
              variant: 'status',
              icon: '✅',
              eyebrow: 'Status',
              title: 'Evidence linked',
              dek: 'Citations remain visible and never turn untrusted content into markup.',
              tone: 'success',
              citations: ['visual-reference'],
            },
            {
              type: 'card',
              variant: 'action',
              icon: 'arrow_up_right',
              eyebrow: 'Next action',
              title: 'Review the rollout checklist',
              content: 'Use the source-linked action card to keep a whole-card destination explicit.',
              href: 'https://example.org/fixture/checklist',
            },
          ],
        },
        {
          type: 'grid',
          columns: 3,
          blocks: [
            {
              // `visual` with a resolvable image renders a full-bleed
              // photo/illustration background behind the title/summary,
              // like a hero banner sized for a grid tile.
              type: 'card',
              variant: 'visual',
              icon: 'activity',
              eyebrow: 'Image-forward',
              title: 'Visual card with media',
              summary: 'The image becomes a full-bleed background with a legibility gradient behind the title and summary.',
              href: 'https://example.org/fixture/visual',
              media: {
                href: '/fixtures/daily-pulse-lovosice.svg',
                alt: 'Abstract blue editorial illustration, full-bleed card variant',
                credit: 'Cognis fixture',
                aspect_ratio: '16 / 7',
                focal_point: '50% 50%',
              },
              source_ids: ['visual-reference'],
            },
            {
              // `visual` without an authored `media` field gracefully
              // degrades to the same flat card layout as every other
              // variant instead of rendering an empty overlay shell.
              type: 'card',
              variant: 'visual',
              icon: 'info',
              eyebrow: 'Image-forward, no media',
              title: 'Visual card without media',
              summary: 'Without an authored image, this falls back to the standard flat card treatment.',
            },
            {
              // `visual` with a `media` reference that fails to load (a
              // deliberately broken path) also falls back to the flat
              // layout once the browser reports the load failure, instead
              // of leaving light-on-dark text with no image behind it.
              type: 'card',
              variant: 'visual',
              icon: 'info',
              eyebrow: 'Image-forward, broken media',
              title: 'Visual card with a broken image',
              summary: 'A failed image load also falls back to the flat card treatment.',
              media: {
                href: '/fixtures/does-not-exist.svg',
                alt: 'Intentionally missing fixture image',
              },
            },
          ],
        },
        {
          type: 'grid',
          columns: 3,
          blocks: [
            { type: 'metric', icon: 'trend_up', label: 'Mobile coverage', value: '320–430px', delta: '+3 viewports', timestamp: 'Updated now', tone: 'success' },
            { type: 'metric', icon: 'clock', label: 'Median scan time', value: '14s', delta: '−18%', timestamp: 'This week', tone: 'neutral' },
            { type: 'metric', icon: 'unknown_icon_name', label: 'Safe fallback', value: 'No markup', delta: 'Unknown icon omitted', tone: 'warning' },
          ],
        },
        {
          type: 'chart',
          title: 'Visual system adoption',
          description: 'The SVG baseline remains useful before Chart.js enhancement.',
          spec_version: 'cognis.chart.v1',
          chart_type: 'line',
          series: [{
            id: 'adopted-surfaces',
            label: 'Adopted surfaces',
            points: [
              { x: '2026-07-13', y: 2 },
              { x: '2026-07-14', y: 4 },
              { x: '2026-07-15', y: 7 },
            ],
          }],
          x_axis: { type: 'time', label: 'Weekday' },
          y_axis: { type: 'linear', label: 'Surfaces', unit: 'surfaces', min: 0, max: 8 },
          legend_position: 'none',
          palette_token: 'cool',
        },
        {
          type: 'accordion',
          items: [
            {
              type: 'section',
              title: 'What changed',
              summary: 'The summary stays visible while the contextual notes and evidence remain opt-in.',
              content: 'Expanded editorial context avoids repeating the section title and can include supporting citations.',
              blocks: [{ type: 'source_list', title: 'Supporting evidence' }],
            },
          ],
        },
      ],
      assets: [],
      sources: [{ id: 'visual-reference', title: 'Cognis visual system fixture', url: 'https://example.org/fixture/visual-system', publisher: 'Cognis' }],
      datasets: [],
      exports: [],
    },
  },
  {
    id: 'id-collision-report',
    title: 'ID collision report',
    description: 'Reserved namespaces, duplicate explicit IDs, and titled Markdown hierarchy.',
    content: 'Collision fixture fallback.',
    payload: {
      metadata: { toc: { enabled: true, depth: 3 } },
      blocks: [
        { type: 'section', id: 'rich-section-0', title: 'Legacy collision' },
        { type: 'section', id: 'reference-1', title: 'Reference collision' },
        { type: 'section', id: 'references-heading', title: 'Bibliography collision' },
        { type: 'section', id: 'cite-1-1', title: 'Citation collision' },
        { type: 'section', id: 'toc', title: 'TOC collision' },
         { type: 'figure', id: 'figure-1', title: 'Figure collision' },
         { type: 'table', id: 'table-1', title: 'Table collision' },
         { type: 'section', id: 'mermaid-0', title: 'Mermaid ID collision' },
         { type: 'mermaid', title: 'Actual Mermaid', source: 'flowchart LR\n  A --> B' },
        { type: 'section', id: 'duplicate', title: 'Duplicate one' },
        { type: 'section', id: 'duplicate', title: 'Duplicate two' },
        {
          type: 'markdown',
          id: 'summary',
          title: 'Summary',
          content: '# Content heading\n\nParagraph.\n\n### Detail heading\n\nMore.',
        },
        {
          type: 'research_answer',
          title: 'Evidence',
          paragraphs: [{ text: 'Claim.', citations: ['source'] }],
        },
      ],
      assets: [],
      sources: [{ id: 'source', title: 'Collision source' }],
      datasets: [],
      exports: [],
    },
  },
  {
    id: 'every-block-reference',
    title: 'Every block type reference',
    description: 'One instance of every block type in SUPPORTED_RICH_BLOCK_TYPES, grouped by composition family, for design-system QA.',
    content: 'Every-block reference fallback for design-system QA.',
    payload: {
      metadata: {
        eyebrow: 'Design system QA',
        subtitle: 'Every registered block type, grouped by composition family.',
      },
      blocks: [
        { type: 'hero', eyebrow: 'Feline field report', title: 'Every rich block, investigated by cats', subtitle: 'A cheerful QA surface: practical renderer coverage, questionable cat productivity.' },

        { type: 'section', title: 'Status at a glance', blocks: [
          { type: 'dashboard', title: 'Household cat operations', metrics: [
            { label: 'Naps completed', value: '14', delta: '+2 since lunch', tone: 'success' },
            { label: 'Laser-pointer budget', value: '42%', tone: 'warning' },
          ] },
          { type: 'status', status: 'Treat drawer secured', description: 'A critical control is holding despite sustained inspection.' },
          { type: 'status_grid', metrics: [
            { label: 'Window perch', value: 'Occupied', tone: 'success' },
            { label: 'Cardboard box', value: 'Contested', tone: 'warning' },
          ] },
          { type: 'action', title: 'Open the treat protocol', href: 'https://en.wikipedia.org/wiki/Cat', content: 'One explicit next step: deploy snack.' },
          { type: 'metric', label: 'Door-opening response', value: '0.4s', delta: 'faster than human reflexes', tone: 'success' },
          { type: 'card_grid', blocks: [
            { type: 'card', title: 'Operational finding', content: 'The phrase “pspsps” has a significantly higher response rate near food.' },
            { type: 'card', title: 'Risk register', content: 'An unattended glass of water remains at elevated tail-sweep risk.' },
          ] },
        ] },

        { type: 'section', title: 'Narrative with evidence', blocks: [
          { type: 'research_answer', title: 'Can cats recognize their names?', description: 'The short answer: often yes. The operational response is negotiable.', paragraphs: [
            { text: 'Cats can distinguish their own names from similar words, especially when spoken by familiar people.', sources: ['ref-a'] },
            { text: 'A lack of response is not strong evidence of confusion; it may be a deliberate prioritization of sunbeam maintenance.', sources: ['ref-a'] },
          ], key_points: ['Familiar voices improve recognition.', 'Selective hearing is a feature, not a bug.'], source_ids: ['ref-a'] },
          { type: 'evidence_report', title: 'Weighed claims', claims: [
            { label: 'Primary', title: 'Purring is not only a happiness signal', content: 'Cats also purr when self-soothing or asking for attention.', confidence: 0.86,
              evidence: [{ text: 'Context matters: posture, eyes, and timing add the useful signal.', source: 'cat-behaviour-notes' }] },
          ] },
          { type: 'claim_cards', title: 'Claim cards alias', claims: [
            { label: 'Alias', title: 'Cats have more than 20 muscles in each ear', content: 'A cat can rotate an ear toward interesting sounds with remarkable precision.', confidence: 0.91 },
          ] },
          { type: 'quote', content: 'In ancient times cats were worshipped as gods; they have not forgotten this.' },
        ] },

        { type: 'section', title: 'Comparison and decision', blocks: [
          { type: 'table', title: 'Cat activity forecast', columns: [{ key: 'time', label: 'Time' }, { key: 'activity', label: 'Likely activity' }], rows: [{ time: '03:17', activity: 'Hallway sprinting' }, { time: '09:00', activity: 'Breakfast negotiation' }] },
          { type: 'comparison_matrix', title: 'Comparison matrix', columns: [
            { key: 'option', label: 'Sleeping venue' }, { key: 'cost', label: 'Comfort score' },
          ], rows: [{ option: 'Sunny chair', cost: 'Excellent' }, { option: 'Laptop keyboard', cost: 'Strategic' }] },
          { type: 'decision_matrix', title: 'Decision matrix', columns: [
            { key: 'option', label: 'Toy' }, { key: 'verdict', label: 'Verdict' },
          ], rows: [{ option: 'Crinkly paper', verdict: 'Recommended', recommended: true }, { option: 'Expensive plush mouse', verdict: 'Ignored' }] },
        ] },

        { type: 'section', title: 'Sequence and process', blocks: [
          { type: 'timeline', title: 'A morning in cat time', items: [
            { time: '05:42', title: 'Breakfast escalation', content: 'The bowl is only 18% empty, a clear emergency.' },
            { time: '06:04', title: 'Resolution', content: 'Human acknowledges service-level objective.' },
          ] },
          { type: 'steps', title: 'How to earn a cat’s approval', items: [
            { step: '1', title: 'Sit down', content: 'Become a warm, stationary piece of furniture.' },
            { step: '2', title: 'Verify', content: 'Wait for lap occupancy; do not move.' },
          ] },
          { type: 'day_agenda', eyebrow: 'Thursday, July 16', title: 'Agenda', now: '2026-07-16T08:20:00+02:00', timezone: 'Europe/Prague', all_day: ['Keep cardboard box under observation'], items: [
            { start: '2026-07-16T09:00:00+02:00', end: '2026-07-16T09:30:00+02:00', title: 'Sunbeam quality review', location: 'South window', description: 'Verify warmth and bird visibility before occupying the chair.' },
            { start: '2026-07-16T11:30:00+02:00', end: '2026-07-16T12:00:00+02:00', title: 'Lunch negotiation', description: 'Escalate gently until a snack service-level objective is acknowledged.' },
          ], tasks: [{ title: 'Inspect every grocery bag on arrival' }, { title: 'Audit the new cardboard delivery' }] },
          { type: 'incident_timeline', title: 'Incident timeline', items: [
            { time: '10:42', title: 'Vacuum cleaner detected', content: 'Immediate evacuation to the secure under-bed zone.', tone: 'danger' },
          ] },
          { type: 'incident_checklist', title: 'Remediation', checklist: [
            { title: 'Confirm no cucumber is present', owner: 'Muchi', done: true },
            { title: 'Reclaim cardboard box', owner: 'Muchi', done: false },
          ] },
          { type: 'checklist', title: 'Checklist alias', checklist: [
            { title: 'Inspect every grocery bag on arrival', done: false },
          ] },
        ] },

        { type: 'section', title: 'Prose and structure', blocks: [
          { type: 'markdown', content: 'Cats have a **Jacobson’s organ** in the roof of the mouth. That odd open-mouth face after a smell is called the [flehmen response](https://en.wikipedia.org/wiki/Flehmen_response).' },
          { type: 'stack', blocks: [
            { type: 'markdown', content: 'A cat’s whiskers are sensitive spatial sensors.' },
            { type: 'markdown', content: 'That is why a narrow bowl can be surprisingly offensive.' },
          ] },
          { type: 'columns', blocks: [
            { type: 'markdown', content: 'Left paw: unplanned keyboard input.' },
            { type: 'markdown', content: 'Right paw: rigorous quality assurance.' },
          ] },
          { type: 'grid', blocks: [
            { type: 'metric', label: 'Whiskers', value: '24' },
            { type: 'metric', label: 'Toe beans', value: '18' },
            { type: 'metric', label: 'Concern for calendars', value: '0%' },
          ] },
        ] },

        { type: 'section', title: 'Visual evidence', blocks: [
          { type: 'chart', title: 'Zoomies by weekday', description: 'A minimal line chart for design-system QA.', spec_version: 'cognis.chart.v1', chart_type: 'line',
            series: [{ id: 'zoomies', label: 'Zoomie intensity', points: [{ x: 'Mon', y: 1 }, { x: 'Tue', y: 3 }, { x: 'Wed', y: 2 }] }],
            x_axis: { type: 'category', label: 'Weekday' },
            y_axis: { type: 'linear', label: 'Intensity', unit: 'laps', min: 0, max: 4 },
            legend_position: 'none', palette_token: 'default',
            source: 'The Institute of Extremely Serious Cat Science', observed_at: '2026-07-16T08:00:00+00:00' },
          { type: 'figure', alt: 'An orange cat sleeping in a transparent illustration', caption: 'Cats spend much of the day sleeping, apparently to preserve energy for exactly one dramatic hallway sprint.', src: catNapFigure },
          { type: 'gallery', blocks: [
            { type: 'figure', alt: 'A cat birdwatching at a window', caption: 'Birdwatching is premium entertainment.', src: catGalleryWindow },
            { type: 'figure', alt: 'A cat in a cardboard box', caption: 'The box has passed inspection.', src: catGalleryBox },
          ] },
          { type: 'mermaid', title: 'Treat delivery flow', source: 'flowchart LR\n  A[Human opens drawer] --> B[Cat appears instantly]\n  B --> C{Treat supplied?}\n  C -->|Yes| D[Purr]\n  C -->|No| E[Escalate meow]' },
        ] },

        { type: 'section', title: 'Reference and code', blocks: [
          { type: 'code', title: 'Snippet', language: 'python', content: 'def fix():\n    return True' },
          { type: 'kv', title: 'Key values', items: [{ label: 'Owner', value: 'Team A' }, { label: 'Priority', value: 'High' }] },
          { type: 'key_value', title: 'key_value alias', items: [{ label: 'Alias', value: 'Confirmed' }] },
          { type: 'source_list', title: 'Sources' },
          { type: 'link', title: 'Feline enrichment guide', url: 'https://example.org', description: 'A concise reference for better birdwatching ergonomics.', thumbnail: catGalleryWindow, thumbnail_alt: 'Cat watching birds from a window' },
          { type: 'link_preview', site: 'Cat field notes', title: 'The cardboard box quality standard', url: 'https://example.org', description: 'A preview card with an optional author-supplied thumbnail.', thumbnail: catGalleryBox, thumbnail_alt: 'Cat inspecting a cardboard box' },
        ] },

        { type: 'section', title: 'Emphasis, sparingly', blocks: [
          { type: 'callout', tone: 'success', title: 'One highlight', content: 'Exactly one true highlight per deliverable.' },
          { type: 'divider' },
        ] },

        { type: 'section', title: 'Containers', blocks: [
          { type: 'tabs', items: [
            { type: 'markdown', title: 'Tab one', content: 'First tab content.' },
            { type: 'markdown', title: 'Tab two', content: 'Second tab content.' },
          ] },
          { type: 'accordion', items: [
            { type: 'markdown', title: 'Accordion one', content: 'First accordion content.' },
            { type: 'markdown', title: 'Accordion two', content: 'Second accordion content.' },
          ] },
          { type: 'modal', title: 'Open modal', items: [
            { type: 'markdown', content: 'Modal content.' },
          ] },
        ] },
      ],
      sources: [{ id: 'ref-a', title: 'Reference source' }],
      assets: [],
      datasets: [],
      exports: [],
    },
  },
];

export const richDeliverableVisualFixture = richDeliverableVisualScenarios[0];
