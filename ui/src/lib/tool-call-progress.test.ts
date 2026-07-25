import { describe, expect, it } from 'vitest';

import type { ToolCallTimelineItem } from '$lib/timeline-render-model';
import {
  formatPatchPreparationProgressLabel,
  shouldShowPatchPreparationProgress,
} from '$lib/tool-call-progress';

function toolCall(overrides: Partial<ToolCallTimelineItem> = {}): ToolCallTimelineItem {
  return {
    id: 'tool:call-1',
    kind: 'tool_call',
    callId: 'call-1',
    toolName: 'apply_patch',
    status: 'running',
    timestamp: null,
    ...overrides,
  };
}

describe('tool call progress', () => {
  it('shows apply_patch preparation progress while the runtime tool is running', () => {
    const item = toolCall({
      progressPhase: 'preparing_input',
      progressInputLines: 8,
      progressInputChars: 120,
      progressComplete: false,
    });

    expect(shouldShowPatchPreparationProgress(item)).toBe(true);
    expect(formatPatchPreparationProgressLabel(item)).toBe('Preparing patch (8 lines, 120 chars)');
  });

  it('keeps supporting the pre-runtime started status', () => {
    expect(shouldShowPatchPreparationProgress(toolCall({
      status: 'started',
      progressPhase: 'preparing_input',
    }))).toBe(true);
  });

  it('hides apply_patch preparation progress once patch text arguments are available', () => {
    expect(shouldShowPatchPreparationProgress(toolCall({
      progressPhase: 'preparing_input',
      progressInputLines: 8,
      progressInputChars: 120,
      arguments: { patchText: '*** Begin Patch\n*** End Patch\n' },
    }))).toBe(false);
  });

  it('hides patch preparation progress for terminal or unrelated tools', () => {
    expect(shouldShowPatchPreparationProgress(toolCall({
      status: 'completed',
      progressPhase: 'preparing_input',
    }))).toBe(false);
    expect(shouldShowPatchPreparationProgress(toolCall({
      toolName: 'bash',
      progressPhase: 'preparing_input',
    }))).toBe(false);
  });
});
