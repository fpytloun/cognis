import type { SlashCommandSuggestion } from '$lib/types/api';

type StaticSlashCommand = {
  command: string;
  description: string;
  acceptsArgument?: boolean;
  parameterSuggestions?: boolean;
};

export type ChatModeDirective = {
  mode: 'default' | 'plan' | 'build';
  oneShot: boolean;
  content: string | null;
};

const STATIC_SLASH_COMMANDS: StaticSlashCommand[] = [
  { command: '/help', description: 'Show available commands' },
  { command: '/model', description: 'List or switch LLM model', acceptsArgument: true, parameterSuggestions: true },
  { command: '/thinking', description: 'Set reasoning effort', acceptsArgument: true, parameterSuggestions: true },
  { command: '/profile', description: 'List or switch agent runtime profile', acceptsArgument: true, parameterSuggestions: true },
  { command: '/skill', description: 'List or load a skill into this session', acceptsArgument: true, parameterSuggestions: true },
  { command: '/executor', description: 'Show or switch active executor', acceptsArgument: true, parameterSuggestions: true },
  { command: '/context', description: 'Show context usage' },
  { command: '/info', description: 'Show session details' },
  { command: '/lsp', description: 'Show LSP diagnostics status' },
  { command: '/plan', description: 'Plan/read-only mode; add text for one-shot planning', acceptsArgument: true },
  { command: '/build', description: 'Build/implementation mode; add text for one-shot build', acceptsArgument: true },
  { command: '/default', description: 'Return to agent default mode; add text for one-shot default', acceptsArgument: true },
  { command: '/compact', description: 'Compact conversation' },
  { command: '/summarize', description: 'Alias for /compact' },
  { command: '/fork', description: 'Fork conversation; add text to start the fork', acceptsArgument: true },
  { command: '/task', description: 'Create a background task', acceptsArgument: true },
  { command: '/research', description: 'Start a research task', acceptsArgument: true },
  { command: '/implement', description: 'Start an implementation task', acceptsArgument: true },
  { command: '/delegate', description: 'Delegate a bounded sub-task', acceptsArgument: true },
  { command: '/undo', description: 'Undo the last user turn in this chat' },
  { command: '/redo', description: 'Redo the last undone turn in this chat' },
  { command: '/new', description: 'Start new conversation' },
  { command: '/reset', description: 'Alias for /new' },
  { command: '/clear', description: 'Alias for /new' },
  { command: '/stop', description: 'Stop current work' },
  { command: '/cancel', description: 'Alias for /stop' },
  { command: '/approve', description: 'Approve tool escalation', acceptsArgument: true },
  { command: '/deny', description: 'Deny tool escalation', acceptsArgument: true },
  { command: '/retry', description: 'Retry paused workflow gate', acceptsArgument: true },
  { command: '/continue', description: 'Continue paused workflow gate', acceptsArgument: true }
];

export const SYSTEM_SLASH_COMMANDS = STATIC_SLASH_COMMANDS.map((item) => item.command);

const PARAMETER_SUGGESTION_COMMANDS = new Set(
  STATIC_SLASH_COMMANDS.filter((item) => item.parameterSuggestions).map((item) => item.command)
);
const PREFIX_SYSTEM_COMMANDS = new Set([
  '/fork',
  '/model',
  '/thinking',
  '/profile',
  '/skill',
  '/executor',
  '/task',
  '/research',
  '/implement',
  '/delegate',
  '/approve',
  '/deny',
  '/retry',
  '/continue',
  '/stop',
  '/cancel'
]);

export function normalizeSlashCommandInput(value: string): string {
  const trimmed = value.trim();
  if (!trimmed.startsWith('/')) return trimmed;
  return `/${trimmed.slice(1).trimStart()}`;
}

export function isSystemSlashCommand(value: string): boolean {
  const normalized = normalizeSlashCommandInput(value);
  return SYSTEM_SLASH_COMMANDS.some((command) => {
    if (!PREFIX_SYSTEM_COMMANDS.has(command)) return normalized === command;
    return normalized === command || normalized.startsWith(`${command} `);
  });
}

export function parseChatModeDirectiveInput(value: string): ChatModeDirective | null {
  const normalized = normalizeSlashCommandInput(value);
  for (const mode of ['default', 'plan', 'build'] as const) {
    const command = `/${mode}`;
    if (normalized === command) {
      return { mode, oneShot: false, content: null };
    }
    if (normalized.startsWith(`${command} `)) {
      const content = normalized.slice(command.length).trim();
      if (content) return { mode, oneShot: true, content };
    }
  }
  return null;
}

export function localSlashCommandSuggestions(input: string, limit = 12): SlashCommandSuggestion[] {
  const normalized = normalizeSlashCommandInput(input);
  const hasTrailingWhitespace = input.length > 0 && /\s$/.test(input);
  if (!normalized.startsWith('/') || normalized.includes(' ') || hasTrailingWhitespace || normalized.length >= 40) {
    return [];
  }
  const filter = normalized.toLowerCase();
  return STATIC_SLASH_COMMANDS
    .filter((item) => item.command.startsWith(filter))
    .slice(0, limit)
    .map((item) => ({
      kind: 'command',
      command: item.command,
      value: item.command,
      label: item.command,
      description: item.description,
      insert_text: item.acceptsArgument ? `${item.command} ` : item.command,
      suffix: item.acceptsArgument ? 'space' : 'none',
      badges: []
    }));
}

export function slashParameterSuggestionCommand(input: string): string | null {
  const normalized = normalizeSlashCommandInput(input);
  if (!normalized.startsWith('/')) return null;
  const hasTrailingSpace = input.length > 0 && /\s$/.test(input);
  if (hasTrailingSpace && PARAMETER_SUGGESTION_COMMANDS.has(normalized)) {
    return normalized;
  }
  const firstSpace = normalized.indexOf(' ');
  if (firstSpace < 0) return null;
  const command = normalized.slice(0, firstSpace);
  if (!PARAMETER_SUGGESTION_COMMANDS.has(command)) return null;
  const partial = normalized.slice(firstSpace + 1).trim();
  if (partial.includes(' ')) return null;
  return command;
}

export function applySlashSuggestion(suggestion: SlashCommandSuggestion): string {
  if (suggestion.suffix === 'space' && !suggestion.insert_text.endsWith(' ')) {
    return `${suggestion.insert_text} `;
  }
  return suggestion.insert_text;
}
