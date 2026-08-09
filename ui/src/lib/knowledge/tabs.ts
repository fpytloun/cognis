export type KnowledgeTab = 'browse' | 'search' | 'documents' | 'access' | 'settings';

const TABS = new Set<KnowledgeTab>(['browse', 'search', 'documents', 'access', 'settings']);

export function resolveKnowledgeTab(
  requested: string | null,
  accessLevel: 'owner' | 'shared',
  isViewer: boolean
): KnowledgeTab {
  if (!requested || !TABS.has(requested as KnowledgeTab)) return 'browse';
  if ((requested === 'access' || requested === 'settings') && (accessLevel !== 'owner' || isViewer)) {
    return 'browse';
  }
  return requested as KnowledgeTab;
}
