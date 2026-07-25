import type {
  ExecutorConfig,
  LocalModelCatalogItem,
  LocalModelDeploymentCreate,
  LocalModelFitMetadata,
  LocalModelFitPlan,
  LocalModelFitStatus,
  LocalModelSelector
} from '$lib/types/api';

export const CONTEXT_PRESETS = [8192, 16384, 32768, 65536, 131072, 262144] as const;

export type CapacityZone = 'green' | 'yellow' | 'red' | 'unknown';

export function managedHostDisabledReason(executor: ExecutorConfig): string | null {
  if (executor.status !== 'active') return 'Executor is disabled.';
  if (executor.local_inference_enabled === false) {
    return 'Local inference is disabled. Enable it in executor settings and reconnect.';
  }
  if (executor.ollama_management_enabled === false) {
    return 'Model management is disabled. Enable “Allow Cognis to manage Ollama models” in executor settings and reconnect.';
  }
  if (
    executor.runtime_state &&
    !['online', 'ready', 'active'].includes(executor.runtime_state)
  ) {
    return 'Executor is unreachable. Reconnect it before selecting this host.';
  }
  if (executor.resource_snapshot?.ollama?.status === 'unreachable') {
    return 'Ollama is unreachable on this executor.';
  }
  return null;
}

export function contextPresets(advertisedMax: number | null | undefined): number[] {
  const values: number[] = [...CONTEXT_PRESETS];
  if (advertisedMax != null && advertisedMax > 0 && !values.includes(advertisedMax)) {
    values.push(advertisedMax);
  }
  return [...new Set(values)].sort((left, right) => left - right);
}

export function fitMetadata(
  model: LocalModelCatalogItem,
  requestedRef: string
): LocalModelFitMetadata {
  const quantization = model.quantizations.find((item) => item.requested_ref === requestedRef);
  const selectedSize = quantization ? quantization.size_bytes : model.file_size_bytes;
  return {
    requested_ref: requestedRef,
    weights_bytes: selectedSize,
    file_size_bytes: selectedSize,
    parameter_count: model.parameter_count,
    quantization: quantization?.name ?? null,
    bits_per_weight: quantization?.bits_per_weight ?? null,
    layer_count: model.architecture.layer_count ?? null,
    kv_head_count: model.architecture.kv_head_count ?? null,
    head_dimension: model.architecture.head_dimension ?? null,
    advertised_max_context: model.advertised_max_context
  };
}

export function matchedExecutors(
  executors: ExecutorConfig[],
  selector: LocalModelSelector
): ExecutorConfig[] {
  const ids = new Set(selector.executor_ids ?? []);
  const labels = Object.entries(selector.match_labels ?? {});
  return executors.filter(
    (executor) =>
      ids.has(executor.executor_id) ||
      (labels.length > 0 &&
        labels.every(([key, value]) => executor.labels[key] === value))
  );
}

export function statusZone(status: LocalModelFitStatus): CapacityZone {
  if (status === 'FIT') return 'green';
  if (status === 'FIT_WITH_OFFLOAD') return 'yellow';
  if (status === 'NO_FIT') return 'red';
  return 'unknown';
}

export function planZone(plan: LocalModelFitPlan | null): CapacityZone {
  if (!plan || plan.executors.some((result) => result.admission.status === 'UNKNOWN')) {
    return 'unknown';
  }
  if (plan.advertised_max_exceeded || plan.executors.some((result) => result.admission.status === 'NO_FIT')) {
    return 'red';
  }
  if (plan.executors.some((result) => result.admission.status === 'FIT_WITH_OFFLOAD')) {
    return 'yellow';
  }
  return 'green';
}

export function requiresCapacityOverride(plan: LocalModelFitPlan | null): boolean {
  const zone = planZone(plan);
  return zone === 'red' || zone === 'unknown';
}

export function deploymentPayload(
  requestedRef: string,
  selector: LocalModelSelector,
  providerId: string,
  plan: LocalModelFitPlan,
  overrideAcknowledged: boolean
): LocalModelDeploymentCreate {
  const overrideRequired = requiresCapacityOverride(plan);
  return {
    requested_ref: requestedRef,
    selector,
    provider_id: providerId,
    capacity_override_acknowledged: overrideRequired && overrideAcknowledged,
    capacity_assessment_generation: plan.assessment_generation
  };
}

export function friendlyFitStatus(status: LocalModelFitStatus): string {
  if (status === 'FIT') return 'Fits now';
  if (status === 'FIT_WITH_OFFLOAD') return 'Fits with CPU offload';
  if (status === 'NO_FIT') return 'Probably will not load';
  return 'Not enough information';
}

export function formatContext(tokens: number | null | undefined): string {
  if (tokens == null) return '—';
  if (tokens >= 1024 && tokens % 1024 === 0) return `${tokens / 1024}k`;
  return tokens.toLocaleString();
}
