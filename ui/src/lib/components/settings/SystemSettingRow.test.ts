import { fireEvent, render, screen } from '@testing-library/svelte';
import { describe, expect, it, vi } from 'vitest';

import type { Setting } from '$lib/types/api';
import SystemSettingRow from './SystemSettingRow.svelte';

function setting(overrides: Partial<Setting> = {}): Setting {
  return {
    key: 'executors.allow_subprocess',
    value: true,
    category: 'executors',
    section: 'Local execution',
    label: 'Allow subprocess executors',
    description: 'Allows local child-process executors.',
    default_value: true,
    value_type: 'boolean',
    options: null,
    minimum: null,
    maximum: null,
    unit: null,
    is_overridden: true,
    apply_scope: 'immediate',
    updated_by: 'admin@example.com',
    updated_at: '2026-07-13T10:00:00Z',
    ...overrides
  };
}

describe('SystemSettingRow', () => {
  it('renders the current value, persistent information action, override state, and explicit actions', () => {
    render(SystemSettingRow, {
      setting: setting(),
      draft: 'true',
      onchange: vi.fn(),
      onapply: vi.fn(),
      onreset: vi.fn()
    });

    expect(screen.getByRole('checkbox', { name: 'Allow subprocess executors' })).toBeChecked();
    expect(screen.getByRole('button', { name: 'Information about Allow subprocess executors' })).toBeTruthy();
    expect(screen.getByText('Customized')).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Reset to default' })).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Apply' })).toBeDisabled();
  });

  it('keeps list items individually keyboard-editable', async () => {
    const onchange = vi.fn();
    render(SystemSettingRow, {
      setting: setting({
        key: 'security.allowed_hosts',
        value: ['localhost'],
        label: 'Allowed hosts',
        value_type: 'string_list',
        is_overridden: false
      }),
      draft: '["localhost"]',
      onchange,
      onapply: vi.fn(),
      onreset: vi.fn()
    });

    const input = screen.getByRole('textbox', { name: 'Allowed hosts item 1' });
    await fireEvent.input(input, { target: { value: 'cognis.local' } });
    expect(onchange).toHaveBeenCalledWith('["cognis.local"]');
    expect(screen.getByRole('button', { name: 'Add item' })).toBeTruthy();
  });

  it('shows inline validation and enables Apply only for a dirty draft', () => {
    render(SystemSettingRow, {
      setting: setting({
        key: 'session.timeout',
        value: 30,
        label: 'Session timeout',
        value_type: 'integer',
        minimum: 5,
        maximum: 120,
        unit: 'minutes',
        is_overridden: false
      }),
      draft: '2',
      error: 'Value must be at least 5 minutes.',
      onchange: vi.fn(),
      onapply: vi.fn(),
      onreset: vi.fn()
    });

    expect(screen.getByRole('spinbutton', { name: 'Session timeout' })).toHaveAttribute('aria-invalid', 'true');
    expect(screen.getByRole('alert')).toHaveTextContent('Value must be at least 5 minutes.');
    expect(screen.getByRole('button', { name: 'Apply' })).toBeEnabled();
  });
});
