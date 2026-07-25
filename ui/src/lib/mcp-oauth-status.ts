export type MCPOAuthStatus = {
  connected: boolean;
  authorized?: boolean;
  runtime_connected?: boolean;
  authorization_required?: boolean;
  invalid?: boolean;
  outcome_unknown?: boolean;
  issuer?: string | null;
  resource?: string | null;
  scopes?: string[];
  expires_at?: string | null;
  access_token_expires_at?: string | null;
  authorization_expires_at?: string | null;
  refreshable?: boolean;
  refresh_state?: string;
  refresh_failure_count?: number;
  next_refresh_attempt_at?: string | null;
  last_refresh_error_code?: string | null;
  last_refresh_error_description?: string | null;
  last_refresh_error_at?: string | null;
  status?: string;
};

function formatDateTime(value: string): string {
  return new Date(value).toLocaleString();
}

export function isMcpOAuthStatusCritical(status: MCPOAuthStatus | undefined): boolean {
  if (!status) {
    return false;
  }
  if (!status.connected || status.runtime_connected === false) {
    return true;
  }
  if (status.authorization_expires_at && new Date(status.authorization_expires_at).getTime() <= Date.now()) {
    return true;
  }
  if (!status.refreshable && status.access_token_expires_at && new Date(status.access_token_expires_at).getTime() <= Date.now()) {
    return true;
  }
  return false;
}

export function formatMcpOAuthStatus(status: MCPOAuthStatus | undefined): string {
  if (!status) {
    return 'Status not loaded';
  }
  if (!status.connected) {
    if (status.outcome_unknown || status.refresh_state === 'outcome_unknown') {
      return 'MCP server is not functional: the refresh outcome is unknown and the refresh token may have rotated. Re-authenticate before retrying.';
    }
    if (status.invalid || status.refresh_state === 'invalid') {
      const reason = status.last_refresh_error_code ? ` (${status.last_refresh_error_code})` : '';
      return `MCP server is not functional: OAuth authorization is invalid${reason}. Re-authenticate to use this server.`;
    }
    if (status.refresh_state === 'retry_backoff') {
      const retryAt = status.next_refresh_attempt_at
        ? ` Retry is scheduled for ${formatDateTime(status.next_refresh_attempt_at)}.`
        : '';
      return `MCP server is not functional: token refresh failed temporarily${status.last_refresh_error_code ? ` (${status.last_refresh_error_code})` : ''}.${retryAt}`;
    }
    if (status.status === 'revoked') {
      return 'MCP server is not functional: OAuth authorization was disconnected. Re-authenticate to use this server.';
    }
    if (status.authorization_expires_at && new Date(status.authorization_expires_at).getTime() <= Date.now()) {
      return `MCP server is not functional: refresh authorization expired at ${formatDateTime(status.authorization_expires_at)}. Re-authenticate to use this server.`;
    }
    if (!status.refreshable && status.access_token_expires_at && new Date(status.access_token_expires_at).getTime() <= Date.now()) {
      return `MCP server is not functional: access token expired at ${formatDateTime(status.access_token_expires_at)} and no refresh token is available. Re-authenticate to use this server.`;
    }
    if (status.authorized && status.refresh_state === 'refresh_due') {
      return 'OAuth authorization is refreshable, but the access token is expired and runtime refresh is pending.';
    }
    return `MCP server is not functional: not connected${status.status ? ` (${status.status})` : ''}. Re-authenticate to use this server.`;
  }

  const details: string[] = [
    status.runtime_connected === false ? 'Authorized · runtime reconnect pending' : 'Connected'
  ];
  const accessTokenExpiresAt = status.access_token_expires_at;
  if (accessTokenExpiresAt) {
    details.push(`access token valid until ${formatDateTime(accessTokenExpiresAt)}`);
  }
  const authorizationExpiresAt = status.authorization_expires_at;
  if (authorizationExpiresAt) {
    details.push(`authorization refreshable until ${formatDateTime(authorizationExpiresAt)}`);
  } else if (status.refreshable) {
    details.push('authorization refreshable; no refresh expiry reported');
  } else if (accessTokenExpiresAt) {
    details.push('manual authorization required after access token expires');
  }

  return details.join(' · ');
}
