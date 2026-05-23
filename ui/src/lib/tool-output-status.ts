type ToolOutputStatusFields = {
  status?: string;
  truncated?: boolean;
  agentVisibleTruncated?: boolean;
  transportTruncated?: boolean;
  outputSize?: number;
  hasFullOutput?: boolean;
  recoveryCallId?: string | null;
  liveOutputAvailable?: boolean;
  anchorsAvailable?: boolean;
  anchorCount?: number;
};

export function isToolOutputShortened(
  item: ToolOutputStatusFields,
): boolean {
  return Boolean(item.truncated || item.agentVisibleTruncated || item.transportTruncated);
}

export function toolOutputShorteningMessage(item: ToolOutputStatusFields): string | null {
  return null;
}

export function canOpenToolOutput(item: ToolOutputStatusFields): boolean {
  if (item.liveOutputAvailable) return true;
  return isToolOutputShortened(item) && Boolean(item.recoveryCallId || item.hasFullOutput);
}

export function toolOutputOpenLabel(item: ToolOutputStatusFields): string {
  return item.status === 'started' || item.status === 'running' ? 'Open live output' : 'Open full output';
}

export function legacyToolOutputShorteningMessage(item: ToolOutputStatusFields): string | null {
  if (!isToolOutputShortened(item)) return null;

  const parts = ['Output shortened for chat'];
  if (item.outputSize) {
    parts.push(`original size ${item.outputSize.toLocaleString()} chars`);
  }

  let message = `${parts.join('; ')}.`;
  if (item.hasFullOutput && item.recoveryCallId) {
    message += ` Full output is recoverable with call ID ${item.recoveryCallId}.`;
  }
  if (item.anchorsAvailable && item.anchorCount) {
    message += ` ${item.anchorCount.toLocaleString()} anchors available.`;
  }
  return message;
}
