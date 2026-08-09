import type { KnowledgebaseCapabilities } from '$lib/types/api';

export interface KnowledgebaseReadiness {
  canRead: boolean;
  canMutateCrud: boolean;
  canIngest: boolean;
  canSearch: boolean;
  canAsk: boolean;
  degraded: boolean;
}

export interface KnowledgebaseLifecycleAccess {
  canManageLifecycle: boolean;
  canMutateContent: boolean;
  canIngest: boolean;
}

export function knowledgebaseReadiness(
  capabilities: KnowledgebaseCapabilities | null,
  isViewer: boolean
): KnowledgebaseReadiness {
  const canRead = capabilities?.enabled === true;
  const canSearch =
    canRead && capabilities.backend_ready === true && capabilities.embedding_ready === true;
  const canIngest = canRead && !isViewer && capabilities.indexer_ready === true;
  const canAsk = canSearch && capabilities.ask_ready === true;
  return {
    canRead,
    canMutateCrud: canRead && !isViewer,
    canIngest,
    canSearch,
    canAsk,
    degraded:
      canRead &&
      (!capabilities.backend_ready ||
        !capabilities.embedding_ready ||
        !capabilities.indexer_ready ||
        !capabilities.ask_ready)
  };
}

export function knowledgebaseLifecycleAccess(
  status: string,
  readiness: KnowledgebaseReadiness,
  accessLevel: 'owner' | 'shared' = 'owner'
): KnowledgebaseLifecycleAccess {
  const active = status === 'active';
  const owner = accessLevel === 'owner';
  return {
    canManageLifecycle: owner && readiness.canMutateCrud,
    canMutateContent: owner && active && readiness.canMutateCrud,
    canIngest: owner && active && readiness.canIngest
  };
}
