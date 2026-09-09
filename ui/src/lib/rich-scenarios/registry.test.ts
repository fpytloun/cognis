import { describe, expect, it } from 'vitest';

import { SUPPORTED_RICH_BLOCK_TYPES, richBlockRenderPlan } from '../rich-deliverable';
import { findRichScenarioBlock } from './blocks';
import { richScenarioDocPreviews } from './docs-previews';
import { defaultRichScenarioId, requireRichScenario, richGalleryScenarios, richScenarios } from './registry';

describe('rich scenario registry', () => {
  it('loads unique, deterministically ordered canonical scenarios', () => {
    expect(requireRichScenario(defaultRichScenarioId).id).toBe(defaultRichScenarioId);
    expect(new Set(richScenarios.map((scenario) => scenario.id)).size).toBe(richScenarios.length);
    expect(richScenarios).toEqual(richScenarios.slice().sort(
      (left, right) => left.sort_key - right.sort_key || left.id.localeCompare(right.id),
    ));
    expect(richGalleryScenarios.length).toBeGreaterThan(0);
  });

  it('normalizes every payload without unsupported blocks', () => {
    for (const scenario of richScenarios) {
      expect(richBlockRenderPlan(scenario.payload).filter((entry) => entry.fallback), scenario.id).toEqual([]);
    }
  });

  it('resolves every documentation preview from a canonical scenario', () => {
    const previewIds = richScenarioDocPreviews.map((preview) => preview.id);
    expect(new Set(previewIds).size).toBe(previewIds.length);
    for (const preview of richScenarioDocPreviews) {
      const scenario = requireRichScenario(preview.scenario_id);
      expect(
        findRichScenarioBlock(scenario.payload.blocks, preview.block_type, preview.occurrence ?? 0),
        preview.id,
      ).not.toBeNull();
    }
  });

  it('provides one default documentation preview for every supported block', () => {
    const defaults = richScenarioDocPreviews.filter((preview) => preview.variant === undefined);
    expect(new Set(defaults.map((preview) => preview.block_type))).toEqual(SUPPORTED_RICH_BLOCK_TYPES);
    expect(defaults).toHaveLength(SUPPORTED_RICH_BLOCK_TYPES.size);
  });
});
