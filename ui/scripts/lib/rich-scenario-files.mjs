import { readFile, readdir } from 'node:fs/promises';
import path from 'node:path';

const scenarioDir = new URL('../../src/lib/rich-scenarios/scenarios/', import.meta.url);
export const RICH_SCENARIO_WIDTHS = [390, 768, 1280, 1440];

export async function loadRichScenarioFiles() {
  const filenames = (await readdir(scenarioDir))
    .filter((filename) => filename.endsWith('.json'))
    .sort();
  const scenarios = await Promise.all(filenames.map(async (filename) => {
    const scenario = JSON.parse(await readFile(new URL(filename, scenarioDir), 'utf8'));
    if (scenario.id !== path.basename(filename, '.json')) {
      throw new Error(`${filename}: filename does not match scenario ID ${scenario.id}`);
    }
    return scenario;
  }));
  return scenarios.sort((left, right) => left.sort_key - right.sort_key || left.id.localeCompare(right.id));
}

export async function selectRichScenarios({ group, ids } = {}) {
  const scenarios = (await loadRichScenarioFiles())
    .filter((scenario) => (scenario.visibility ?? ['gallery']).includes('gallery'));
  const selectedIds = ids ? new Set(ids) : null;
  const selected = scenarios.filter((scenario) =>
    (!group || scenario.group === group) && (!selectedIds || selectedIds.has(scenario.id))
  );
  if (selectedIds) {
    const missing = [...selectedIds].filter((id) => !selected.some((scenario) => scenario.id === id));
    if (missing.length) throw new Error(`Unknown or excluded scenario IDs: ${missing.join(', ')}`);
  }
  if (selected.length === 0) throw new Error(`No scenarios matched group ${group ?? 'all'}`);
  return selected;
}
