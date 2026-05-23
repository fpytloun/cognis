import { describe, expect, it } from 'vitest';

import { canOpenToolOutput, isToolOutputShortened, toolOutputOpenLabel, toolOutputShorteningMessage } from '$lib/tool-output-status';

describe('tool output status helpers', () => {
  it('does not treat recoverable full output as shortened', () => {
    const item = {
      outputSize: 117,
      hasFullOutput: true,
      recoveryCallId: 'call_1',
    };

    expect(isToolOutputShortened(item)).toBe(false);
    expect(toolOutputShorteningMessage(item)).toBeNull();
  });

  it('does not render a separate warning bar after inline-marker UX', () => {
    expect(toolOutputShorteningMessage({
      agentVisibleTruncated: true,
      outputSize: 117_000,
      hasFullOutput: true,
      recoveryCallId: 'call_1',
    })).toBeNull();
  });

  it('opens drawer only for live output or shortened recoverable output', () => {
    expect(canOpenToolOutput({ hasFullOutput: true, recoveryCallId: 'call_1' })).toBe(false);
    expect(canOpenToolOutput({
      truncated: true,
      hasFullOutput: true,
      recoveryCallId: 'call_1',
    })).toBe(true);
    expect(canOpenToolOutput({ status: 'started' })).toBe(false);
    expect(canOpenToolOutput({ status: 'running' })).toBe(false);
    expect(canOpenToolOutput({ status: 'started', liveOutputAvailable: true })).toBe(true);
  });

  it('labels running output separately from completed full output', () => {
    expect(toolOutputOpenLabel({ status: 'started' })).toBe('Open live output');
    expect(toolOutputOpenLabel({ status: 'completed' })).toBe('Open full output');
  });
});
