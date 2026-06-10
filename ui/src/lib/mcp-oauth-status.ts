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

export function formatMcpOAuthStatus(status: MCPOAuthStatus | undefined): string {
  if (!status) {
    return 'Status not loaded';
  }
  if (!status.connected) {
    return `Not connected${status.status ? ` (${status.status})` : ''}`;
  }

  const details: string[] = ['Connected'];
  const authorizationExpiresAt = status.authorization_expires_at;
  if (authorizationExpiresAt) {
    details.push(`authorization valid until ${new Date(authorizationExpiresAt).toLocaleString()}`);
  } else if (status.refreshable) {
    details.push('authorization refreshable');
  }

  return details.join(' · ');
}
