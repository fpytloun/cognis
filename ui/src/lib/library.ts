/** Human Library contracts. Empty scope IDs select the unassigned folder. */
export type LibraryKind = 'deliverable' | 'artifact';
export type LibraryGrouping = 'none' | 'project' | 'conversation' | 'project_conversation';
export interface LibraryFilters {
  kind?: LibraryKind;
  q?: string;
  project_id?: string;
  conversation_id?: string;
  source_id?: string;
  agent_id?: string;
  origin?: 'all' | 'user' | 'agent' | 'other';
  ownership?: 'all' | 'mine' | 'shared';
  format?: string;
  after?: string;
  before?: string;
  sort?: 'updated' | 'created';
  favorites?: boolean;
}
export interface LibraryItem {
  id: string;
  kind: LibraryKind;
  title: string | null;
  format: string;
  owner_email: string | null;
  author_email?: string | null;
  author_agent_id?: string | null;
  agent_id: string | null;
  agent_name: string | null;
  project_id: string | null;
  project_name: string | null;
  conversation_id: string | null;
  conversation_title: string | null;
  task_id: string | null;
  task_title: string | null;
  source_id?: string | null;
  source_title?: string | null;
  lineage_id: string | null;
  revision: number;
  created_at: string;
  updated_at: string;
  size_bytes: number;
  status: string;
  origin: 'user' | 'agent' | 'other';
  is_readonly: boolean;
  is_favorite?: boolean;
  /** Open comments on any revision; only reported to the owner. */
  open_comments?: number;
}
export interface LibraryGroup { id: string; label: string; count: number }
export interface LibraryPage { items: LibraryItem[]; next_cursor: string | null }
export interface LibraryGroupsPage { groups: LibraryGroup[]; next_cursor: string | null }

export function libraryQuery(params: LibraryFilters & { cursor?: string; group_by?: string; limit?: number }): string {
  const query = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined && value !== null) query.set(key, String(value));
  }
  return query.toString();
}

export function libraryHref(item: Pick<LibraryItem, 'kind' | 'id'>): string {
  return `/library/${item.kind}/${encodeURIComponent(item.id)}`;
}

export function originLabel(origin: LibraryItem['origin']): string {
  return origin === 'user' ? 'User upload' : origin === 'agent' ? 'Agent output' : 'File';
}

export function formatLabel(format: string): string {
  const known: Record<string, string> = {
    rich: 'Rich document', markdown: 'Markdown', plain: 'Text', html: 'HTML',
    'application/pdf': 'PDF', 'text/markdown': 'Markdown', 'text/plain': 'Text',
    'application/json': 'JSON',
  };
  return known[format] ?? format.split('/').pop()?.toUpperCase() ?? format;
}
