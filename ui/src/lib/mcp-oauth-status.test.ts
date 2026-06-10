import { describe, expect, it } from 'vitest';

import { formatMcpOAuthStatus } from './mcp-oauth-status';

describe('MCP OAuth status formatting', () => {
  it('does not render access-token expiry as connection lifetime', () => {
    const formatted = formatMcpOAuthStatus({
      connected: true,
      refreshable: true,
      access_token_expires_at: '2026-06-08T09:29:00Z',
      expires_at: '2026-06-08T09:29:00Z'
    });

    expect(formatted).toBe('Connected · authorization refreshable');
    expect(formatted).not.toContain('access token valid until');
    expect(formatted).not.toContain('Connected until');
  });

  it('uses authorization expiry when it is available', () => {
    const formatted = formatMcpOAuthStatus({
      connected: true,
      refreshable: true,
      access_token_expires_at: '2026-06-08T09:29:00Z',
      authorization_expires_at: '2026-07-08T09:29:00Z'
    });

    expect(formatted).toContain('Connected · authorization valid until');
    expect(formatted).not.toContain('access token valid until');
  });
});
