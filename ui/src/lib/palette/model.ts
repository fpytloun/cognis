import { settingsTabVisible, type SettingsTab } from '$lib/settings-navigation';
import { availableNavigationItems } from '$lib/navigation';

export const entityKinds = ['agent', 'project', 'conversation', 'task', 'schedule', 'deliverable', 'artifact'] as const;
export type EntityKind = typeof entityKinds[number];
export const resultTypes = [...entityKinds, 'page', 'action', 'setting'] as const;
export function isEntityKind(kind: PaletteItem['kind']): kind is EntityKind {
  return (entityKinds as readonly string[]).includes(kind);
}
export function isWindowKind(kind: PaletteItem['kind']): kind is Exclude<EntityKind, 'agent' | 'project'> {
  return ['conversation', 'task', 'schedule', 'artifact', 'deliverable'].includes(kind);
}
export const typeLabels: Record<typeof resultTypes[number], string> = {
  conversation: 'Conversations', task: 'Tasks', schedule: 'Schedules',
  deliverable: 'Documents', artifact: 'Files', action: 'Actions', setting: 'Settings',
  agent: 'Main agents', project: 'Projects', page: 'App pages',
};
export interface PaletteItem {
  kind: EntityKind | 'setting' | 'action' | 'page';
  id: string;
  title: string;
  project_id: string | null;
  agent_id: string | null;
  agent_name?: string | null;
  agent_avatar_url?: string | null;
  project_avatar_url?: string | null;
  project_icon?: string | null;
  project_color?: string | null;
  origin?: 'agent' | 'user' | 'other' | null;
  producer_name?: string | null;
  status: string | null;
  format: string | null;
  updated_at: string;
  href: string;
  size_bytes?: number;
}
export interface PalettePage { items: PaletteItem[]; next_cursor: string | null }
export const identity = (item: PaletteItem): string => `${item.kind}:${item.id}`;
export const normalize = (value: string): string => value.normalize('NFD').replace(/\p{Diacritic}/gu, '').toLocaleLowerCase();
export const labels: Record<PaletteItem['kind'], string> = {
  conversation: 'Conversation', task: 'Task', schedule: 'Schedule',
  deliverable: 'Document', artifact: 'File', setting: 'Settings', action: 'Action',
  agent: 'Main conversation', project: 'Project', page: 'App page',
};
export function matches(title: string, query: string): boolean {
  const text = normalize(title);
  return normalize(query).trim().split(/\s+/).every((word) => text.includes(word));
}
export function destinationLabel(item: PaletteItem): string {
  return ({ conversation: 'Chat', task: 'Tasks', schedule: 'Schedules', artifact: 'Library',
    deliverable: 'Library', setting: 'Settings', action: 'Chat',
    agent: 'Chat', project: 'Projects', page: item.title })[item.kind];
}
export function shouldOpenWindow(item: PaletteItem, scope: { projectId: string | null } | null, forceRoute: boolean): boolean {
  return !forceRoute && scope !== null
    && isWindowKind(item.kind)
    && (scope.projectId === null || scope.projectId === item.project_id);
}

const settingTabs: readonly (readonly [SettingsTab, string, string])[] = [
  ['providers', 'Providers', 'models llm api'],
  ['routing', 'Routes', 'routing inference models defaults'],
  ['display', 'Display', 'appearance theme dark light'],
  ['notifications', 'Notifications', 'alerts push sounds'],
  ['account', 'Account', 'profile password'],
  ['secrets', 'Credentials', 'secrets tokens'],
  ['tools', 'Tools', 'mcp servers'],
  ['executors', 'Executors', 'devices workers'],
  ['web', 'Web', 'search browser'],
  ['system', 'System', 'configuration'],
  ['users', 'Users', 'accounts permissions'],
] as const;
export function localItems(query: string, admin: boolean, canCreate = true, usageEnabled = true): PaletteItem[] {
  const common = { project_id: null, agent_id: null, status: null, format: null, updated_at: '' };
  const actions: PaletteItem[] = canCreate ? [{
    ...common, kind: 'action', id: 'new-conversation', title: 'New conversation', href: '/chat/new',
  }] : [];
  const pages: PaletteItem[] = query.trim() ? availableNavigationItems(usageEnabled)
    .filter(item => matches(`${item.label} ${item.aliases}`, query))
    .map(item => ({ ...common, kind: 'page', id: item.href, title: item.label, href: item.href })) : [];
  const settings: PaletteItem[] = settingTabs
    .filter(([id, title, aliases]) => settingsTabVisible(id, admin) && matches(`${title} ${aliases}`, query))
    .map(([id, title]) => ({ ...common, kind: 'setting', id, title, href: `/settings?tab=${id}` }));
  return [...pages, ...actions.filter((item) => matches(`${item.title} new chat`, query)), ...query.trim() ? settings : []];
}

/** Literal segments, never HTML: result titles are untrusted. */
export function highlighted(title: string, query: string): { text: string; match: boolean }[] {
  const terms = normalize(query).match(/[\p{L}\p{N}]+/gu) ?? [];
  if (!terms.length) return [{ text: title, match: false }];
  const normalized: string[] = [];
  const offsets: number[] = [];
  let offset = 0;
  for (const char of title) {
    for (const unit of normalize(char)) { normalized.push(unit); offsets.push(offset); }
    offset += char.length;
  }
  const text = normalized.join('');
  const marked = new Set<number>();
  for (const term of terms) {
    let at = text.indexOf(term);
    while (at >= 0) {
      for (let i = at; i < at + term.length; i++) marked.add(offsets[i]);
      at = text.indexOf(term, at + term.length);
    }
  }
  const result: { text: string; match: boolean }[] = [];
  offset = 0;
  for (const char of title) {
    const match = marked.has(offset);
    const last = result.at(-1);
    if (last?.match === match) last.text += char;
    else result.push({ text: char, match });
    offset += char.length;
  }
  return result;
}
