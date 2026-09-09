import { SUPPORTED_RICH_BLOCK_TYPES } from '$lib/rich-deliverable';

import type { RichScenarioPreview } from './schema';

const guideFor = (blockType: string): 'layout' | 'data' =>
  new Set([
    'hero', 'section', 'stack', 'columns', 'grid', 'tabs', 'accordion', 'modal',
    'markdown', 'callout', 'card', 'card_grid', 'quote', 'divider', 'figure',
    'gallery', 'timeline', 'steps', 'section_header',
  ]).has(blockType) ? 'layout' : 'data';

export const richScenarioDocPreviews: RichScenarioPreview[] = [
  ...Array.from(SUPPORTED_RICH_BLOCK_TYPES, (blockType) => ({
    id: blockType,
    scenario_id: blockType === 'section_header' ? 'docs-preview-variants' : 'every-block-reference',
    block_type: blockType,
    guide: guideFor(blockType),
    filename: `${blockType}.png`,
  })),
  {
    id: 'card-visual',
    scenario_id: 'docs-preview-variants',
    block_type: 'card',
    variant: 'visual',
    guide: 'layout',
    filename: 'card-visual.png',
  },
  ...(['line', 'bar', 'donut', 'stacked_bar'] as const).map((variant, occurrence) => ({
    id: `chart-${variant}`,
    scenario_id: 'docs-preview-variants',
    block_type: 'chart',
    occurrence,
    variant,
    guide: 'data' as const,
    filename: `chart-${variant}.png`,
  })),
  {
    id: 'markdown-list-markers',
    scenario_id: 'docs-preview-variants',
    block_type: 'markdown',
    variant: 'list-markers',
    guide: 'layout',
    filename: 'markdown-list-markers.png',
  },
  {
    id: 'dashboard-native-metrics',
    scenario_id: 'docs-preview-variants',
    block_type: 'dashboard',
    variant: 'native-metrics',
    guide: 'data',
    filename: 'dashboard-native-metrics.png',
  },
];

export function getRichScenarioDocPreview(id: string): RichScenarioPreview | undefined {
  return richScenarioDocPreviews.find((preview) => preview.id === id);
}
