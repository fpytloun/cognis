import { fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import type { Setting, SettingsCategory } from '$lib/types/api';
import SystemSettingsEditor from './SystemSettingsEditor.svelte';

const mocks = vi.hoisted(() => ({
  list: vi.fn(),
  update: vi.fn(),
  reset: vi.fn(),
  addToast: vi.fn(),
  confirmAction: vi.fn()
}));

vi.mock('$lib/api/client', () => ({
  api: {
    settings: {
      list: mocks.list,
      update: mocks.update,
      reset: mocks.reset
    }
  },
  asApiError: (error: unknown) => error instanceof Error ? error : new Error(String(error))
}));

vi.mock('$lib/stores/toasts', () => ({ addToast: mocks.addToast }));
vi.mock('$lib/stores/confirm', () => ({ confirmAction: mocks.confirmAction }));

function setting(overrides: Partial<Setting> = {}): Setting {
  return {
    key: 'session.name',
    value: 'current',
    category: 'session',
    section: 'General',
    label: 'Session name',
    description: 'Default session name.',
    default_value: 'default',
    value_type: 'string',
    options: null,
    minimum: null,
    maximum: null,
    unit: null,
    is_overridden: true,
    apply_scope: 'new sessions',
    updated_by: null,
    updated_at: null,
    ...overrides
  };
}

function categories(item: Setting): SettingsCategory[] {
  return [{ category: item.category, items: [item] }];
}

describe('SystemSettingsEditor', () => {
  beforeEach(() => {
    mocks.list.mockReset();
    mocks.update.mockReset();
    mocks.reset.mockReset();
    mocks.addToast.mockReset();
    mocks.confirmAction.mockReset();
  });

  it('preserves a dirty row when refreshed settings props arrive', async () => {
    const ondirtychange = vi.fn();
    const { rerender } = render(SystemSettingsEditor, {
      settings: categories(setting()),
      onsettingschange: vi.fn(),
      ondirtychange
    });

    const input = screen.getByRole('textbox', { name: 'Session name' });
    await fireEvent.input(input, { target: { value: 'draft value' } });
    await rerender({
      settings: categories(setting({ value: 'remote value' })),
      onsettingschange: vi.fn(),
      ondirtychange
    });

    expect(screen.getByRole('textbox', { name: 'Session name' })).toHaveValue('draft value');
    expect(screen.getByText('Unsaved')).toBeTruthy();
    expect(ondirtychange).toHaveBeenCalledWith(true);
  });

  it('applies one row and emits only the returned local row refresh', async () => {
    const updated = setting({ value: 'saved value', updated_by: 'admin@example.com' });
    mocks.update.mockResolvedValueOnce(updated);
    const onsettingschange = vi.fn();
    render(SystemSettingsEditor, {
      settings: categories(setting()),
      onsettingschange
    });

    await fireEvent.input(screen.getByRole('textbox', { name: 'Session name' }), {
      target: { value: 'saved value' }
    });
    await fireEvent.click(screen.getByRole('button', { name: 'Apply' }));

    expect(mocks.update).toHaveBeenCalledWith('session.name', 'saved value');
    expect(onsettingschange).toHaveBeenCalledWith(categories(updated));
    expect(mocks.list).not.toHaveBeenCalled();
  });

  it('does not let an older background refresh supersede an applied row', async () => {
    let resolveRefresh!: (value: SettingsCategory[]) => void;
    mocks.list.mockReturnValueOnce(new Promise((resolve) => {
      resolveRefresh = resolve;
    }));
    const updated = setting({ value: 'saved value' });
    mocks.update.mockResolvedValueOnce(updated);
    const onsettingschange = vi.fn();
    render(SystemSettingsEditor, {
      settings: categories(setting()),
      onsettingschange
    });

    window.dispatchEvent(new FocusEvent('focus'));
    await waitFor(() => expect(mocks.list).toHaveBeenCalledTimes(1));
    await fireEvent.input(screen.getByRole('textbox', { name: 'Session name' }), {
      target: { value: 'saved value' }
    });
    await fireEvent.click(screen.getByRole('button', { name: 'Apply' }));
    resolveRefresh(categories(setting({ value: 'stale value' })));
    await Promise.resolve();

    expect(onsettingschange).toHaveBeenCalledTimes(1);
    expect(onsettingschange).toHaveBeenCalledWith(categories(updated));
  });

  it('does not start a background refresh while a row mutation is pending', async () => {
    let resolveUpdate!: (value: Setting) => void;
    mocks.update.mockReturnValueOnce(new Promise((resolve) => {
      resolveUpdate = resolve;
    }));
    render(SystemSettingsEditor, {
      settings: categories(setting()),
      onsettingschange: vi.fn()
    });

    await fireEvent.input(screen.getByRole('textbox', { name: 'Session name' }), {
      target: { value: 'saved value' }
    });
    await fireEvent.click(screen.getByRole('button', { name: 'Apply' }));
    window.dispatchEvent(new FocusEvent('focus'));

    expect(mocks.list).not.toHaveBeenCalled();
    resolveUpdate(setting({ value: 'saved value' }));
  });

  it('removes a touched draft after the value is restored to the server value', async () => {
    const ondirtychange = vi.fn();
    render(SystemSettingsEditor, {
      settings: categories(setting()),
      onsettingschange: vi.fn(),
      ondirtychange
    });

    const input = screen.getByRole('textbox', { name: 'Session name' });
    await fireEvent.input(input, { target: { value: 'temporary' } });
    await fireEvent.input(input, { target: { value: 'current' } });
    await waitFor(() => expect(ondirtychange).toHaveBeenLastCalledWith(false));

    expect(screen.queryByText('Unsaved')).toBeNull();
  });

  it('removes a draft when an accepted refresh adopts the same value', async () => {
    const adopted = setting({ value: 'draft value' });
    mocks.list.mockResolvedValueOnce(categories(adopted));
    const onsettingschange = vi.fn();
    const { rerender } = render(SystemSettingsEditor, {
      settings: categories(setting()),
      onsettingschange
    });

    await fireEvent.input(screen.getByRole('textbox', { name: 'Session name' }), {
      target: { value: 'draft value' }
    });
    window.dispatchEvent(new FocusEvent('focus'));
    await waitFor(() => expect(onsettingschange).toHaveBeenCalledWith(categories(adopted)));
    await rerender({
      settings: categories(adopted),
      onsettingschange
    });
    await rerender({
      settings: categories(setting({ value: 'later remote value' })),
      onsettingschange
    });

    expect(screen.getByRole('textbox', { name: 'Session name' })).toHaveValue('later remote value');
    expect(screen.queryByText('Unsaved')).toBeNull();
  });

  it('confirms reset and emits the returned default row without a full reload', async () => {
    const reset = setting({ value: 'default', is_overridden: false });
    mocks.confirmAction.mockResolvedValueOnce(true);
    mocks.reset.mockResolvedValueOnce(reset);
    const onsettingschange = vi.fn();
    render(SystemSettingsEditor, {
      settings: categories(setting()),
      onsettingschange
    });

    await fireEvent.click(screen.getByRole('button', { name: 'Reset to default' }));

    expect(mocks.confirmAction).toHaveBeenCalledOnce();
    expect(mocks.reset).toHaveBeenCalledWith('session.name');
    expect(onsettingschange).toHaveBeenCalledWith(categories(reset));
    expect(mocks.list).not.toHaveBeenCalled();
  });
});
