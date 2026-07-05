import { describe, expect, it } from 'vitest';

import { formatMcpOAuthStatus, isMcpOAuthStatusCritical } from './mcp-oauth-status';

describe('MCP OAuth status formatting', () => {
  it('does not render access-token expiry as connection lifetime', () => {
    const formatted = formatMcpOAuthStatus({
      connected: true,
      refreshable: true,
      access_token_expires_at: '2026-06-08T09:29:00Z',
      expires_at: '2026-06-08T09:29:00Z'
    });

    expect(formatted).toContain('Connected · access token valid until');
    expect(formatted).toContain('authorization refreshable; no refresh expiry reported');
    expect(formatted).not.toContain('Connected until');
  });

  it('uses authorization expiry when it is available', () => {
    const formatted = formatMcpOAuthStatus({
      connected: true,
      refreshable: true,
      access_token_expires_at: '2026-06-08T09:29:00Z',
      authorization_expires_at: '2026-07-08T09:29:00Z'
    });

    expect(formatted).toContain('Connected · access token valid until');
    expect(formatted).toContain('authorization refreshable until');
  });

  it('clearly explains expired refresh authorization', () => {
    const formatted = formatMcpOAuthStatus({
      connected: false,
      refreshable: true,
      status: 'active',
      authorization_expires_at: '2000-01-01T00:00:00Z'
    });

    expect(formatted).toContain('MCP server is not functional');
    expect(formatted).toContain('refresh authorization expired');
    expect(formatted).toContain('Re-authenticate');
    expect(isMcpOAuthStatusCritical({
      connected: false,
      refreshable: true,
      authorization_expires_at: '2000-01-01T00:00:00Z'
    })).toBe(true);
  });
});
