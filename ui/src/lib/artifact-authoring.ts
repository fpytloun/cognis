import type { LibraryItem } from './library';
export interface ArtifactDraft {
  filename: string; mime_type: string; content: string; project_id?: string; agent_ids?: string[];
}
export interface EditableArtifact { artifact_id: string; filename: string; mime_type: string; content: string }
export interface AgentAccess {
  agent_id: string; name: string; explicit: boolean; inherited: boolean; avatar_url?: string | null;
  explicit_permission?: 'read' | 'write' | null;
  inherited_permission?: 'read' | 'write' | null;
  inherited_sources?: string[];
}
export interface DocumentAccess { agents: AgentAccess[]; project_id: string | null; owner_agent_id: string | null }
export interface PublicLink { url: string; mode: 'view' | 'download'; expires_at: string }
export interface PublicLinkOptions { mode: 'view' | 'download'; ttl_seconds?: number; expires_at?: string }
export type ResourceChange = (item: LibraryItem) => void;
