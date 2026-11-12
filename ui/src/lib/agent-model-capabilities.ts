import type { LLMProvider, ModelEntry } from '$lib/types/api';
import type { InferenceRoute } from '$lib/inference-routes';

export type AgentModelCapabilities = Pick<ModelEntry, 'reasoning_efforts' | 'supports_fast_mode'>;

/** Route settings must be valid for every target, including fallback accounts. */
export function agentModelCapabilities(
  providers: LLMProvider[], routes: InferenceRoute[], providerId: string, modelId: string
): AgentModelCapabilities | null {
  const model = modelId.trim();
  if (model.startsWith('cognis/')) {
    const route = routes.find(item => item.reference === model);
    if (!route?.config.targets.length) return null;
    const targets = route.config.targets.map(target =>
      providers.find(provider => provider.provider_id === target.provider_id)
        ?.models.find(entry => entry.model_id === target.model));
    if (targets.some(target => !target)) return null;
    const known = targets as ModelEntry[];
    const efforts = (target: ModelEntry) =>
      target.reasoning_efforts?.length ? target.reasoning_efforts : ['default'];
    return {
      reasoning_efforts: efforts(known[0]).filter(effort =>
        known.every(target => efforts(target).includes(effort))),
      supports_fast_mode: known.every(target => target.supports_fast_mode === true)
    };
  }
  const provider = providers.find(item => item.provider_id === providerId);
  if (!provider) return null;
  const selected = model || (typeof provider.config?.default_model === 'string'
    ? provider.config.default_model : '');
  const entry = provider.models.find(item => item.model_id === selected) ?? null;
  if (!entry || (entry.reasoning_efforts != null && typeof entry.supports_fast_mode === 'boolean')) {
    return entry;
  }
  // Saved custom-provider metadata may omit capabilities; do not mutate that configuration.
  return {
    reasoning_efforts: entry.reasoning_efforts ?? [],
    supports_fast_mode: entry.supports_fast_mode === true
  };
}
