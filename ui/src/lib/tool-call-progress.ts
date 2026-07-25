import { isActiveToolStatus, type ToolCallTimelineItem } from '$lib/timeline-render-model';

function isApplyPatchToolName(toolName: string): boolean {
  const normalized = toolName.toLowerCase().replace(/_/g, '');
  return normalized === 'applypatch' || normalized.endsWith('applypatch');
}

export function shouldShowPatchPreparationProgress(item: ToolCallTimelineItem): boolean {
  return isApplyPatchToolName(item.toolName)
    && isActiveToolStatus(item.status)
    && item.progressPhase === 'preparing_input'
    && !item.arguments?.patchText;
}

export function formatPatchPreparationProgressLabel(item: ToolCallTimelineItem): string {
  const lines = item.progressInputLines;
  const chars = item.progressInputChars;
  const parts: string[] = [];
  if (typeof lines === 'number' && lines > 0) parts.push(`${lines.toLocaleString()} lines`);
  if (typeof chars === 'number' && chars > 0) parts.push(`${chars.toLocaleString()} chars`);
  return parts.length > 0 ? `Preparing patch (${parts.join(', ')})` : 'Preparing patch';
}
