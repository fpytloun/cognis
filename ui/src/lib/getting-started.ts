import type { SystemDiagnostics } from '$lib/types/api';

export interface GettingStartedStep {
  id: string;
  label: string;
  description: string;
  href: string;
  done: boolean;
}

const STORAGE_KEY = 'cognis-getting-started-dismissed';

export function deriveGettingStartedSteps(diagnostics: SystemDiagnostics): GettingStartedStep[] {
  const readiness = diagnostics.readiness ?? {};
  return [
    {
      id: 'services',
      label: 'Check companion services',
      description: 'Make sure Mnemory and Intaris are reachable from Cognis.',
      href: '/settings?tab=system',
      done: Boolean(readiness.mnemory_reachable && readiness.intaris_reachable)
    },
    {
      id: 'providers',
      label: 'Configure LLM provider',
      description: 'Add at least one provider and verify connectivity.',
      href: '/settings?tab=providers',
      done: Boolean(readiness.llm_provider_configured)
    },
    {
      id: 'agents',
      label: 'Create your first agent',
      description: 'Create an agent with identity, tools, and model settings.',
      href: '/agents/new',
      done: Boolean(readiness.agent_created)
    },
    {
      id: 'chat',
      label: 'Start chatting',
      description: 'Open chat once providers and agents are ready.',
      href: '/chat/new',
      done: Boolean(readiness.chat_ready)
    }
  ];
}

export function isGettingStartedDismissed(): boolean {
  return typeof window !== 'undefined' && window.localStorage.getItem(STORAGE_KEY) === '1';
}

export function setGettingStartedDismissed(value: boolean): void {
  if (typeof window === 'undefined') {
    return;
  }
  if (value) {
    window.localStorage.setItem(STORAGE_KEY, '1');
    return;
  }
  window.localStorage.removeItem(STORAGE_KEY);
}
