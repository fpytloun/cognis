import {
  normalizeRichDeliverable,
  richBlockRenderPlan,
  type RichDeliverablePayload,
} from '$lib/rich-deliverable';

import {
  RICH_SCENARIO_SCHEMA_VERSION,
  type RichScenario,
  type RichScenarioVisibility,
} from './schema';

const modules = import.meta.glob('./scenarios/*.json', {
  eager: true,
  import: 'default',
}) as Record<string, unknown>;

function nonEmptyString(value: unknown, field: string, file: string): string {
  if (typeof value !== 'string' || value.trim() === '') {
    throw new Error(`${file}: ${field} must be a non-empty string`);
  }
  return value;
}

function parseVisibility(value: unknown, file: string): RichScenarioVisibility[] {
  if (value === undefined) return ['gallery'];
  if (!Array.isArray(value) || value.length === 0) {
    throw new Error(`${file}: visibility must be a non-empty array`);
  }
  const visibility = value.map((item) => {
    if (item !== 'gallery' && item !== 'docs') {
      throw new Error(`${file}: unsupported visibility ${String(item)}`);
    }
    return item;
  });
  return [...new Set(visibility)];
}

function parseScenario(file: string, value: unknown): RichScenario {
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    throw new Error(`${file}: scenario must be an object`);
  }
  const raw = value as Record<string, unknown>;
  if (raw.schema_version !== RICH_SCENARIO_SCHEMA_VERSION) {
    throw new Error(`${file}: unsupported schema_version ${String(raw.schema_version)}`);
  }
  const id = nonEmptyString(raw.id, 'id', file);
  const filename = file.split('/').pop();
  if (filename !== `${id}.json`) {
    throw new Error(`${file}: filename must match scenario ID ${id}`);
  }
  if (!Number.isInteger(raw.sort_key)) {
    throw new Error(`${file}: sort_key must be an integer`);
  }
  const payload = normalizeRichDeliverable(raw.payload);
  const unsupported = richBlockRenderPlan(payload).filter((entry) => entry.fallback);
  if (unsupported.length > 0) {
    throw new Error(`${file}: unsupported blocks: ${unsupported.map((entry) => entry.type).join(', ')}`);
  }
  return {
    schema_version: RICH_SCENARIO_SCHEMA_VERSION,
    id,
    title: nonEmptyString(raw.title, 'title', file),
    description: nonEmptyString(raw.description, 'description', file),
    group: nonEmptyString(raw.group, 'group', file),
    sort_key: raw.sort_key as number,
    content: nonEmptyString(raw.content, 'content', file),
    visibility: parseVisibility(raw.visibility, file),
    payload: payload as RichDeliverablePayload,
  };
}

const scenarios = Object.entries(modules).map(([file, value]) => parseScenario(file, value));
const scenarioIds = scenarios.map((scenario) => scenario.id);
if (new Set(scenarioIds).size !== scenarioIds.length) {
  throw new Error('Rich scenario IDs must be unique');
}

export const richScenarios = scenarios
  .slice()
  .sort((left, right) => left.sort_key - right.sort_key || left.id.localeCompare(right.id));

export const richGalleryScenarios = richScenarios.filter((scenario) =>
  scenario.visibility.includes('gallery')
);

export const defaultRichScenarioId = 'research-answer';

export function getRichScenario(id: string): RichScenario | undefined {
  return richScenarios.find((scenario) => scenario.id === id);
}

export function requireRichScenario(id: string): RichScenario {
  const scenario = getRichScenario(id);
  if (!scenario) throw new Error(`Unknown rich scenario: ${id}`);
  return scenario;
}
