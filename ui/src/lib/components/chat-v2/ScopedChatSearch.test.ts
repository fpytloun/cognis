import { fireEvent, render, screen } from '@testing-library/svelte';
import { describe, expect, it, vi } from 'vitest';
import { tick } from 'svelte';
import ScopedChatSearch from './ScopedChatSearch.svelte';

describe('scoped chat search', () => {
  it('restores the explicit opener rather than focus from the bar mount', async () => {
    const opener = document.createElement('button');
    const previouslyFocused = document.createElement('button');
    document.body.append(opener, previouslyFocused);
    previouslyFocused.focus();
    const view = render(ScopedChatSearch, { scopeKey: 'session:first', items: [], onSelect() {} });
    await view.component.openSearch(opener);
    await fireEvent.keyDown(screen.getByRole('searchbox'), { key: 'Escape' });
    await tick();
    expect(opener).toHaveFocus();
    opener.remove(); previouslyFocused.remove();
  });
  const items = [
    { id: 'first-message', kind: 'message', role: 'assistant' as const, content: 'Verify the service safely' },
    { id: 'second-message', kind: 'message', role: 'user' as const, content: 'Verify the second service' },
  ];

  it('uses shared local matching, supports navigation and closes with Escape', async () => {
    const onSelect = vi.fn();
    const view = render(ScopedChatSearch, { scopeKey: 'session:first', items, onSelect });
    await view.component.openSearch();
    const input = screen.getByPlaceholderText('Search loaded subsession history');
    await fireEvent.input(input, { target: { value: 'Verify' } });
    await fireEvent.submit(input.closest('form')!);
    expect(onSelect).toHaveBeenLastCalledWith('first-message');
    await fireEvent.click(screen.getByRole('button', { name: 'Next match' }));
    expect(onSelect).toHaveBeenLastCalledWith('second-message');
    await fireEvent.input(input, { target: { value: 'safely' } });
    expect(screen.getByRole('status')).toHaveTextContent('1 of 1');
    await fireEvent.keyDown(input, { key: 'Escape' });
    expect(screen.queryByRole('searchbox')).not.toBeInTheDocument();
  });

  it('discards matches and selection when the displayed scope changes', async () => {
    const props = { scopeKey: 'session:first', items, onSelect: vi.fn() };
    const view = render(ScopedChatSearch, props);
    await view.component.openSearch();
    await fireEvent.input(screen.getByRole('searchbox'), { target: { value: 'Verify' } });
    await view.rerender({ ...props, scopeKey: 'session:second', items: [] });
    expect(screen.queryByRole('searchbox')).not.toBeInTheDocument();
    await view.component.openSearch();
    expect(screen.getByRole('searchbox')).toHaveValue('');
    expect(screen.getByRole('button', { name: 'Next match' })).toBeDisabled();
    expect(props.onSelect).not.toHaveBeenCalled();
  });

  it('preserves the selected message identity when older matches are prepended', async () => {
    const props = { scopeKey: 'session:first', items, onSelect: vi.fn() };
    const view = render(ScopedChatSearch, props);
    await view.component.openSearch();
    await fireEvent.input(screen.getByRole('searchbox'), { target: { value: 'Verify' } });
    await fireEvent.click(screen.getByRole('button', { name: 'Next match' }));
    expect(props.onSelect).toHaveBeenLastCalledWith('second-message');
    await view.rerender({ ...props, items: [
      { ...items[0], id: 'older-message', content: 'Verify the earlier service' }, ...items,
    ] });
    expect(screen.getByRole('status')).toHaveTextContent('3 of 3');
    expect(view.container.querySelector('[aria-current="true"]')).toBeNull();
    expect(props.onSelect).toHaveBeenCalledOnce();
  });
});
