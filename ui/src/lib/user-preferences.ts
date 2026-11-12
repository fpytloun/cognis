import type { UserPreferences } from '$lib/types/api';
import { DEFAULT_THEME_FAMILY, normalizeThemeFamily } from '$lib/theme/catalog';

export const DEFAULT_USER_PREFERENCES: UserPreferences = {
  display: {
    theme: 'system',
    theme_family: DEFAULT_THEME_FAMILY,
    language: 'auto',
    dashboard_workspace_windows: true,
    pinned_agents_collapsed: true,
    pinned_agents_order_by_activity: true,
    conversation_sidebar_sections: true,
    conversation_project_badge: 'name',
    conversation_sidebar_dense: false
  },
  chat: {
    enter_to_send: true,
    adapt_tts: false,
    show_thinking_blocks: false,
    group_tool_calls: true,
    keep_assistant_messages_separate: false,
    show_internal_tool_calls: false,
    auto_recap: true
  },
  notifications: {
    include_content: true
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
  const notifications = isRecord(value.notifications) ? value.notifications : {};
  const theme = display.theme === 'dark' || display.theme === 'light' || display.theme === 'system'
    ? display.theme
    : DEFAULT_USER_PREFERENCES.display.theme;
  const language = typeof display.language === 'string' && display.language.trim()
    ? display.language.trim()
    : DEFAULT_USER_PREFERENCES.display.language;
  return {
    display: {
      theme,
      theme_family: normalizeThemeFamily(display.theme_family),
      language,
      conversation_project_badge: display.conversation_project_badge === 'icon' || display.conversation_project_badge === 'hidden' || display.conversation_project_badge === 'monochrome'
        ? display.conversation_project_badge : 'name',
      dashboard_workspace_windows: typeof display.dashboard_workspace_windows === 'boolean'
        ? display.dashboard_workspace_windows
        : DEFAULT_USER_PREFERENCES.display.dashboard_workspace_windows,
      pinned_agents_collapsed: typeof display.pinned_agents_collapsed === 'boolean'
        ? display.pinned_agents_collapsed : DEFAULT_USER_PREFERENCES.display.pinned_agents_collapsed,
      pinned_agents_order_by_activity: typeof display.pinned_agents_order_by_activity === 'boolean'
        ? display.pinned_agents_order_by_activity : DEFAULT_USER_PREFERENCES.display.pinned_agents_order_by_activity,
      conversation_sidebar_sections: typeof display.conversation_sidebar_sections === 'boolean'
        ? display.conversation_sidebar_sections
        : DEFAULT_USER_PREFERENCES.display.conversation_sidebar_sections,
      conversation_sidebar_dense: typeof display.conversation_sidebar_dense === 'boolean'
        ? display.conversation_sidebar_dense
        : DEFAULT_USER_PREFERENCES.display.conversation_sidebar_dense
    },
    chat: {
      adapt_tts: chat.adapt_tts === true,
      enter_to_send: typeof chat.enter_to_send === 'boolean'
        ? chat.enter_to_send
        : DEFAULT_USER_PREFERENCES.chat.enter_to_send,
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
        : DEFAULT_USER_PREFERENCES.chat.show_internal_tool_calls,
      auto_recap: typeof chat.auto_recap === 'boolean'
        ? chat.auto_recap
        : DEFAULT_USER_PREFERENCES.chat.auto_recap
    },
    notifications: {
      include_content: typeof notifications.include_content === 'boolean'
        ? notifications.include_content
        : DEFAULT_USER_PREFERENCES.notifications.include_content
    }
  };
}
