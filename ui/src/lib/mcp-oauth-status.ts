export type MCPOAuthStatus = {
  connected: boolean;
  issuer?: string | null;
  resource?: string | null;
  scopes?: string[];
  expires_at?: string | null;
  access_token_expires_at?: string | null;
  authorization_expires_at?: string | null;
  refreshable?: boolean;
  status?: string;
};

function formatDateTime(value: string): string {
  return new Date(value).toLocaleString();
}

export function isMcpOAuthStatusCritical(status: MCPOAuthStatus | undefined): boolean {
  if (!status) {
    return false;
  }
  if (!status.connected) {
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
    if (status.status === 'revoked') {
      return 'MCP server is not functional: OAuth authorization was disconnected. Re-authenticate to use this server.';
    }
    if (status.authorization_expires_at && new Date(status.authorization_expires_at).getTime() <= Date.now()) {
      return `MCP server is not functional: refresh authorization expired at ${formatDateTime(status.authorization_expires_at)}. Re-authenticate to use this server.`;
    }
    if (!status.refreshable && status.access_token_expires_at && new Date(status.access_token_expires_at).getTime() <= Date.now()) {
      return `MCP server is not functional: access token expired at ${formatDateTime(status.access_token_expires_at)} and no refresh token is available. Re-authenticate to use this server.`;
    }
    return `MCP server is not functional: not connected${status.status ? ` (${status.status})` : ''}. Re-authenticate to use this server.`;
  }

  const details: string[] = ['Connected'];
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
