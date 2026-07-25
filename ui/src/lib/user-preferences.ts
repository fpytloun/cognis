import type { UserPreferences } from '$lib/types/api';

export const DEFAULT_USER_PREFERENCES: UserPreferences = {
  display: {
    theme: 'system',
    language: 'auto'
  },
  chat: {
    show_thinking_blocks: false,
    group_tool_calls: true,
    keep_assistant_messages_separate: false,
    show_internal_tool_calls: false
  }
};

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

export function normalizeUserPreferences(value: unknown): UserPreferences {
  if (!isRecord(value)) {
    return structuredClone(DEFAULT_USER_PREFERENCES);
  }
  const display = isRecord(value.display) ? value.display : {};
  const chat = isRecord(value.chat) ? value.chat : {};
  const theme = display.theme === 'dark' || display.theme === 'light' || display.theme === 'system'
    ? display.theme
    : DEFAULT_USER_PREFERENCES.display.theme;
  const language = typeof display.language === 'string' && display.language.trim()
    ? display.language.trim()
    : DEFAULT_USER_PREFERENCES.display.language;
  return {
    display: {
      theme,
      language
    },
    chat: {
      show_thinking_blocks: typeof chat.show_thinking_blocks === 'boolean'
        ? chat.show_thinking_blocks
        : DEFAULT_USER_PREFERENCES.chat.show_thinking_blocks,
      group_tool_calls: typeof chat.group_tool_calls === 'boolean'
        ? chat.group_tool_calls
        : DEFAULT_USER_PREFERENCES.chat.group_tool_calls,
      keep_assistant_messages_separate: typeof chat.keep_assistant_messages_separate === 'boolean'
        ? chat.keep_assistant_messages_separate
        : DEFAULT_USER_PREFERENCES.chat.keep_assistant_messages_separate,
      show_internal_tool_calls: typeof chat.show_internal_tool_calls === 'boolean'
        ? chat.show_internal_tool_calls
        : DEFAULT_USER_PREFERENCES.chat.show_internal_tool_calls
    }
  };
}
