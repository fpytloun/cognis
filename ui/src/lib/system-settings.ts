import type { Setting, SettingsCategory } from '$lib/types/api';

export type SettingControlKind = 'boolean' | 'enum' | 'number' | 'string' | 'string-list' | 'json';

export interface SettingSection {
  name: string;
  items: Setting[];
}

export interface SettingCategoryGroup {
  name: string;
  sections: SettingSection[];
}

export interface ParsedSettingDraft {
  value?: unknown;
  error?: string;
}

function normalizedType(setting: Setting): string {
  return setting.value_type.trim().toLowerCase().replaceAll('-', '_');
}

export function settingControlKind(setting: Setting): SettingControlKind {
  if (setting.options && setting.options.length > 0) return 'enum';

  const type = normalizedType(setting);
  if (type === 'boolean' || type === 'bool') return 'boolean';
  if (['integer', 'int', 'number', 'float'].includes(type)) return 'number';
  if (['string_list', 'list_string', 'array_string', 'strings'].includes(type)) return 'string-list';
  if (type === 'string') return 'string';

  if (typeof setting.value === 'boolean') return 'boolean';
  if (typeof setting.value === 'number') return 'number';
  if (typeof setting.value === 'string') return 'string';
  if (Array.isArray(setting.value) && setting.value.every((item) => typeof item === 'string')) {
    return 'string-list';
  }
  return 'json';
}

export function serializeSettingValue(setting: Setting, value: unknown = setting.value): string {
  const kind = settingControlKind(setting);
  if (kind === 'string') return typeof value === 'string' ? value : String(value ?? '');
  if (kind === 'number') return typeof value === 'number' ? String(value) : '';
  if (kind === 'boolean') return value === true ? 'true' : 'false';
  return JSON.stringify(value, null, kind === 'json' ? 2 : 0) ?? 'null';
}

export function parseSettingDraft(setting: Setting, draft: string): ParsedSettingDraft {
  const kind = settingControlKind(setting);

  if (kind === 'string') return { value: draft };
  if (kind === 'boolean') return { value: draft === 'true' };

  if (kind === 'number') {
    if (!draft.trim()) return { error: 'A numeric value is required.' };
    const value = Number(draft);
    if (!Number.isFinite(value)) return { error: 'Enter a valid number.' };
    if (['integer', 'int'].includes(normalizedType(setting)) && !Number.isInteger(value)) {
      return { error: 'Enter a whole number.' };
    }
    if (setting.minimum !== null && value < setting.minimum) {
      return { error: `Value must be at least ${setting.minimum}${setting.unit ? ` ${setting.unit}` : ''}.` };
    }
    if (setting.maximum !== null && value > setting.maximum) {
      return { error: `Value must be at most ${setting.maximum}${setting.unit ? ` ${setting.unit}` : ''}.` };
    }
    return { value };
  }

  try {
    const value = JSON.parse(draft) as unknown;
    if (kind === 'string-list' && (!Array.isArray(value) || value.some((item) => typeof item !== 'string'))) {
      return { error: 'Every list item must be text.' };
    }
    return { value };
  } catch {
    return { error: kind === 'string-list' ? 'The string list is invalid.' : 'Enter valid JSON.' };
  }
}

export function groupSystemSettings(categories: SettingsCategory[]): SettingCategoryGroup[] {
  return categories.map((category) => {
    const sections = new Map<string, Setting[]>();
    for (const setting of category.items) {
      const section = setting.section.trim() || 'General';
      sections.set(section, [...(sections.get(section) ?? []), setting]);
    }
    return {
      name: category.category,
      sections: [...sections.entries()].map(([name, items]) => ({ name, items }))
    };
  });
}

export function replaceSetting(categories: SettingsCategory[], updated: Setting): SettingsCategory[] {
  let replaced = false;
  const next = categories.map((category) => ({
    ...category,
    items: category.items.map((setting) => {
      if (setting.key !== updated.key) return setting;
      replaced = true;
      return updated;
    })
  }));

  if (replaced) return next;
  const categoryIndex = next.findIndex((category) => category.category === updated.category);
  if (categoryIndex >= 0) {
    return next.map((category, index) => index === categoryIndex
      ? { ...category, items: [...category.items, updated] }
      : category);
  }
  return [...next, { category: updated.category, items: [updated] }];
}

export function settingOptionValue(option: unknown): string {
  return JSON.stringify(settingOptionPayload(option)) ?? 'null';
}

export function settingOptionLabel(option: unknown): string {
  if (typeof option === 'string') return option;
  if (option && typeof option === 'object' && !Array.isArray(option)) {
    const record = option as Record<string, unknown>;
    if (typeof record.label === 'string') return record.label;
    if ('value' in record) return String(record.value);
  }
  return String(option);
}

export function settingOptionPayload(option: unknown): unknown {
  if (option && typeof option === 'object' && !Array.isArray(option) && 'value' in option) {
    return (option as Record<string, unknown>).value;
  }
  return option;
}

export function settingInfoText(setting: Setting): string {
  const description = setting.description.trim() || 'No description provided.';
  const defaultValue = JSON.stringify(setting.default_value);
  return `${description}\nDefault: ${defaultValue}\nKey: ${setting.key}\nApplication scope: ${setting.apply_scope}`;
}
