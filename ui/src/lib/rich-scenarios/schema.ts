import type { RichDeliverablePayload } from '$lib/rich-deliverable';

export const RICH_SCENARIO_SCHEMA_VERSION = 1;

export type RichScenarioVisibility = 'gallery' | 'docs';

export interface RichScenario {
  schema_version: typeof RICH_SCENARIO_SCHEMA_VERSION;
  id: string;
  title: string;
  description: string;
  group: string;
  sort_key: number;
  content: string;
  visibility: RichScenarioVisibility[];
  payload: RichDeliverablePayload;
}

export interface RichScenarioPreview {
  id: string;
  scenario_id: string;
  block_type: string;
  occurrence?: number;
  variant?: string;
  guide: 'layout' | 'data';
  filename: string;
}
