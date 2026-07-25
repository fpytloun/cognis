import type { RichDeliverablePayload } from '$lib/rich-deliverable';

export interface RichDeliverableDataScenario {
  id: string;
  title: string;
  description: string;
  content: string;
  payload: RichDeliverablePayload;
}

const today = '2026-07-09';

export const richDeliverableDataScenario: RichDeliverableDataScenario = {
  id: 'interactive-data-dashboard',
  title: 'Interactive operations dashboard',
  description: 'Data-focused fixture for interactive charts, status cards, and incident timelines.',
  content: 'Interactive data dashboard fallback content.',
  payload: {
    metadata: {
      eyebrow: 'Dynamic data',
      subtitle: 'Renderer-owned controls for charts, dashboards, and incident response blocks.',
      badges: ['Chart controls', 'Dashboard', 'Incident'],
    },
    blocks: [
      {
        type: 'hero',
        eyebrow: 'Operations',
        title: 'Model-serving reliability is stable after the routing fix',
        subtitle: 'The error budget recovered while traffic continued to grow.',
        badges: ['Healthy', today, 'No backend JS'],
      },
      {
        type: 'dashboard',
        eyebrow: 'Status',
        title: 'Service health summary',
        description: 'KPI cards use declarative data, local details, and SVG sparklines owned by the renderer.',
        status: 'Healthy',
        tone: 'success',
        metrics: [
          {
            label: 'Availability',
            value: '99.96%',
            delta: '+0.03 pp',
            status: 'Good',
            tone: 'success',
            sparkline: [99.91, 99.92, 99.94, 99.93, 99.96, 99.96, 99.96],
            explanation: 'Availability is above the 99.9% weekly target after the retry storm was contained.',
            drilldown: ['API gateway: 99.98%', 'Agent loop: 99.94%', 'Executor bridge: 99.97%'],
          },
          {
            label: 'P95 latency',
            value: '4.2s',
            delta: '-0.6s',
            status: 'Improving',
            tone: 'success',
            sparkline: [5.3, 5.1, 4.8, 4.6, 4.4, 4.3, 4.2],
            explanation: 'Latency improved after queue backpressure tuning.',
          },
          {
            label: 'Open incidents',
            value: '1',
            delta: '-2',
            status: 'Watch',
            tone: 'warning',
            sparkline: [3, 3, 2, 2, 1, 1, 1],
            explanation: 'One low-severity follow-up remains open for dashboard visual QA.',
          },
        ],
      },
      {
        type: 'chart',
        title: 'Request and error trend',
        description: 'Multi-series time series with renderer-owned range and legend controls.',
        spec_version: 'cognis.chart.v1',
        chart_type: 'line',
        range_selector: [
          { id: '7d', label: '7D' },
          { id: '30d', label: '30D' },
          { id: 'all', label: 'All' },
        ],
        fill_empty_buckets: true,
        series: [
          {
            id: 'requests',
            label: 'Requests',
            points: [
              { x: '2026-06-30', y: 1200 }, { x: '2026-07-01', y: 1330 },
              { x: '2026-07-03', y: 1460 }, { x: '2026-07-04', y: 1520 },
              { x: '2026-07-05', y: 1600 }, { x: '2026-07-06', y: 1710 },
              { x: '2026-07-07', y: 1780 }, { x: '2026-07-08', y: 1810 },
              { x: '2026-07-09', y: 1860 },
            ],
          },
          {
            id: 'errors',
            label: 'Errors',
            points: [
              { x: '2026-06-30', y: 21 }, { x: '2026-07-01', y: 18 },
              { x: '2026-07-03', y: 14 }, { x: '2026-07-04', y: 12 },
              { x: '2026-07-05', y: 8 }, { x: '2026-07-06', y: 7 },
              { x: '2026-07-07', y: 5 }, { x: '2026-07-08', y: 4 },
              { x: '2026-07-09', y: 3 },
            ],
          },
        ],
        x_axis: { type: 'time' },
        y_axis: { type: 'linear' },
      },
      {
        type: 'incident_timeline',
        eyebrow: 'Incident',
        title: 'Retry storm remediation',
        description: 'Expandable timeline entries with severity, status, owner, and remediation checklist.',
        severity: 'P2',
        status: 'Resolved',
        owner: 'Platform',
        items: [
          {
            time: '09:18',
            title: 'Alert fired',
            severity: 'warning',
            status: 'Investigating',
            owner: 'On-call',
            content: 'Error ratio breached the burn-rate threshold after a provider timeout wave.',
            open: true,
          },
          {
            time: '09:31',
            title: 'Traffic shifted',
            severity: 'warning',
            status: 'Mitigating',
            owner: 'Runtime',
            content: 'Requests were shifted away from the degraded provider while queue depth drained.',
          },
          {
            time: '09:54',
            title: 'Recovery confirmed',
            severity: 'success',
            status: 'Resolved',
            owner: 'On-call',
            content: 'Error budget burn returned below threshold and no stuck agent turns remained.',
          },
        ],
        checklist: [
          { title: 'Add provider timeout regression', owner: 'Runtime', status: 'done', done: true },
          { title: 'Publish dashboard interaction QA', owner: 'UI', status: 'in progress' },
          { title: 'Review alert threshold sensitivity', owner: 'SRE', status: 'planned' },
        ],
      },
    ],
    assets: [],
    sources: [],
    datasets: [],
    exports: [],
  },
};

