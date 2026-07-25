import { describe, expect, it } from 'vitest';

import {
  groupSystemSettings,
  parseSettingDraft,
  replaceSetting,
  settingControlKind,
  settingOptionValue
} from '$lib/system-settings';
import type { Setting } from '$lib/types/api';

function setting(overrides: Partial<Setting> = {}): Setting {
  return {
    key: 'session.timeout',
    value: 30,
    category: 'session',
    section: 'Lifecycle',
    label: 'Session timeout',
    description: 'How long sessions remain active.',
    default_value: 30,
    value_type: 'integer',
    options: null,
    minimum: 5,
    maximum: 120,
    unit: 'minutes',
    is_overridden: false,
    apply_scope: 'new sessions',
    updated_by: null,
    updated_at: null,
    ...overrides
  };
}

describe('system settings helpers', () => {
  it('chooses responsive control kinds from metadata and safe value fallbacks', () => {
    expect(settingControlKind(setting({ value_type: 'boolean', value: true }))).toBe('boolean');
    expect(settingControlKind(setting({ value_type: 'string', value: 'value' }))).toBe('string');
    expect(settingControlKind(setting({ value_type: 'string_list', value: ['one'] }))).toBe('string-list');
    expect(settingControlKind(setting({ value_type: 'json', value: { nested: true } }))).toBe('json');
    expect(settingControlKind(setting({ options: ['low', 'high'], value: 'low' }))).toBe('enum');
  });

  it('validates numeric constraints and string-list contents', () => {
    expect(parseSettingDraft(setting(), '4').error).toContain('at least 5 minutes');
    expect(parseSettingDraft(setting(), '12.5').error).toBe('Enter a whole number.');
    expect(parseSettingDraft(setting(), '45')).toEqual({ value: 45 });
    expect(parseSettingDraft(setting({ value_type: 'string_list', value: [] }), '["one", 2]').error)
      .toBe('Every list item must be text.');
  });

  it('groups categories into visible sections and replaces only the refreshed row', () => {
    const first = setting();
    const second = setting({ key: 'session.limit', section: 'Limits', value: 10 });
    const categories = [{ category: 'session', items: [first, second] }];

    expect(groupSystemSettings(categories)).toEqual([{
      name: 'session',
      sections: [
        { name: 'Lifecycle', items: [first] },
        { name: 'Limits', items: [second] }
      ]
    }]);

    const updated = setting({ value: 60, is_overridden: true });
    const replaced = replaceSetting(categories, updated);
    expect(replaced[0].items[0]).toBe(updated);
    expect(replaced[0].items[1]).toBe(second);
  });

  it('uses an enum option payload rather than its display metadata', () => {
    expect(settingOptionValue({ label: 'High', value: 'high' })).toBe('"high"');
  });
});
