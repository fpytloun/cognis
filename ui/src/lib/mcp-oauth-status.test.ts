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

  it('never renders Connected for an expired token waiting on refresh', () => {
    const formatted = formatMcpOAuthStatus({
      connected: false,
      authorized: true,
      runtime_connected: false,
      refreshable: true,
      refresh_state: 'refresh_due',
      access_token_expires_at: '2000-01-01T00:00:00Z',
      status: 'active'
    });

    expect(formatted).toContain('access token is expired');
    expect(formatted).not.toContain('Connected');
    expect(isMcpOAuthStatusCritical({
      connected: false,
      authorized: true,
      runtime_connected: false,
      refresh_state: 'refresh_due'
    })).toBe(true);
  });

  it('requires reauthentication after an indeterminate rotating refresh', () => {
    const formatted = formatMcpOAuthStatus({
      connected: false,
      authorized: false,
      outcome_unknown: true,
      refresh_state: 'outcome_unknown',
      last_refresh_error_code: 'refresh_outcome_unknown'
    });

    expect(formatted).toContain('refresh outcome is unknown');
    expect(formatted).toContain('may have rotated');
    expect(formatted).toContain('Re-authenticate');
  });

  it('shows bounded transient retry without claiming runtime connectivity', () => {
    const formatted = formatMcpOAuthStatus({
      connected: false,
      authorized: true,
      runtime_connected: false,
      refresh_state: 'retry_backoff',
      last_refresh_error_code: 'refresh_backend_unavailable',
      next_refresh_attempt_at: '2026-07-13T11:10:00Z'
    });

    expect(formatted).toContain('failed temporarily');
    expect(formatted).toContain('Retry is scheduled');
    expect(formatted).not.toContain('Connected');
  });
});
