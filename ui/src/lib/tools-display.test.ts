import { describe, expect, it } from 'vitest';

import { displayToolName } from '$lib/tools-display';

describe('displayToolName', () => {
  it('shows only the tool segment for MCP tool names', () => {
    expect(displayToolName('mcp_mfg-portal__alertmanager_alerts')).toBe('alertmanager_alerts');
  });

  it('preserves non-MCP tool names', () => {
    expect(displayToolName('bash')).toBe('bash');
  });
});
