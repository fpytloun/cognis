import Bot from 'lucide-svelte/icons/bot';
import Box from 'lucide-svelte/icons/box';
import BookOpen from 'lucide-svelte/icons/book-open';
import Clock from 'lucide-svelte/icons/clock';
import FolderKanban from 'lucide-svelte/icons/folder-kanban';
import Library from 'lucide-svelte/icons/library';
import Files from 'lucide-svelte/icons/files';
import LayoutDashboard from 'lucide-svelte/icons/layout-dashboard';
import ListTodo from 'lucide-svelte/icons/list-todo';
import MessageSquareText from 'lucide-svelte/icons/message-square-text';
import Radio from 'lucide-svelte/icons/radio';
import Settings from 'lucide-svelte/icons/settings';
import Workflow from 'lucide-svelte/icons/workflow';
import Wrench from 'lucide-svelte/icons/wrench';
import ChartNoAxesCombined from 'lucide-svelte/icons/chart-no-axes-combined';

/** Shared destinations for chrome and the launcher; no second route catalog. */
export const navigationItems = [
  { href: '/', label: 'Dashboard', icon: LayoutDashboard, aliases: 'home control center' },
  { href: '/chat', label: 'Chat', icon: MessageSquareText, aliases: 'conversations messages' },
  { href: '/agents', label: 'Agents', icon: Bot, aliases: 'assistants' },
  { href: '/projects', label: 'Projects', icon: FolderKanban, aliases: 'workspaces' },
  { href: '/usage', label: 'Usage', icon: ChartNoAxesCombined, aliases: 'tokens costs accounting companion performance analytics' },
  { href: '/library', label: 'Library', icon: Files, aliases: 'documents artifacts files' },
  { href: '/tasks', label: 'Tasks', icon: ListTodo, aliases: 'work jobs' },
  { href: '/workflows', label: 'Workflows', icon: Workflow, aliases: 'workflow' },
  { href: '/schedules', label: 'Schedules', icon: Clock, aliases: 'automations recurring' },
  { href: '/docs', label: 'Documentation', icon: BookOpen, aliases: 'help guide' },
  { href: '/tools', label: 'Tools', icon: Wrench, aliases: 'capabilities' },
  { href: '/channels', label: 'Channels', icon: Radio, aliases: 'messaging integrations' },
  { href: '/local-models', label: 'Local Models', icon: Box, aliases: 'ollama' },
  { href: '/knowledge', label: 'Knowledge', icon: Library, aliases: 'knowledgebases' },
  { href: '/settings', label: 'Settings', icon: Settings, aliases: 'preferences configuration' },
] as const;

export function availableNavigationItems(usageEnabled = true) {
  return navigationItems.filter((item) => usageEnabled || item.href !== '/usage');
}
