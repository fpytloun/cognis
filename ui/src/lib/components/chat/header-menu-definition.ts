import { deferredDefinition } from '../ui/deferred-definition';
export interface ChatHeaderMenuItem {
  label: string;
  icon: 'comment' | 'comments' | 'resume' | 'search';
  run: () => void;
  disabled?: boolean;
  checked?: boolean;
}
export const headerMenu = deferredDefinition(() => import('./ChatHeaderMenuPanel.svelte'));
