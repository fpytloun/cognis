export const GENERIC_THINKING_EFFORTS = [
  'default',
  'none',
  'low',
  'medium',
  'high',
  'xhigh',
  'max'
] as const;

export function thinkingEffortLabel(value: string): string {
  if (value === 'default') return 'Default';
  if (value === 'xhigh') return 'XHigh';
  return value.charAt(0).toUpperCase() + value.slice(1);
}
