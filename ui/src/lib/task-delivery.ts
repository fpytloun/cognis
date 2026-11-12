import type { CompletionModeFamily } from '$lib/types/api';

export const COMPLETION_MODE_OPTIONS = [
  { value: 'auto', name: 'Default', description: 'Direct on success, Agent otherwise' },
  // Existing persisted choices retain their Agent behavior and wire value.
  { value: 'default', name: 'Agent', description: 'Agent handles the outcome' },
  { value: 'direct', name: 'Direct', description: 'Result only, without an agent turn' },
] as const satisfies readonly { value: CompletionModeFamily; name: string; description: string }[];

export function completionModeLabel(mode: CompletionModeFamily): string {
  return COMPLETION_MODE_OPTIONS.find((option) => option.value === mode)?.name ?? mode;
}

export function resolveCompletionMode(value: unknown): CompletionModeFamily {
  return COMPLETION_MODE_OPTIONS.find((option) => option.value === value)?.value ?? 'auto';
}
