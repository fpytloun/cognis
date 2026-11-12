import { cleanup, fireEvent, render, screen } from '@testing-library/svelte';
import { afterEach, expect, it, vi } from 'vitest';
import UsageFilters from './UsageFilters.svelte';
import { fixtureQuery } from './fixtures';
import type { UsageView } from './model';

afterEach(cleanup);

it('keeps active filters visible and clears only the chosen filter with incomplete facets', async () => {
  const onquery = vi.fn();
  const query = { ...fixtureQuery, filters: { ...fixtureQuery.filters, provider: 'OpenAI', agent: 'Research agent' } };
  // An incomplete facet response (only model returned) must not drop other filters.
  render(UsageFilters, { query, facets: { model: [] } as unknown as UsageView['facets'], onquery });
  expect(screen.getByRole('combobox', { name: 'Filter provider' }).querySelector('option:checked')?.textContent).toBe('OpenAI');
  expect(screen.getByLabelText('Active usage filters').textContent).toContain('Research agent');
  await fireEvent.click(screen.getByRole('button', { name: 'Clear provider filter' }));
  expect(onquery).toHaveBeenCalledExactlyOnceWith({ ...query, filters: { ...query.filters, provider: '' } });
});

it('keeps the immutable project scope in normal flow despite global positioning utilities', () => {
  const utilities = document.createElement('style');
  utilities.textContent = '.fixed { position: fixed; }';
  document.head.append(utilities);
  try {
    render(UsageFilters, { query: {
      ...fixtureQuery, mode: 'project',
      projectScope: { id: 'usage-filter-project', label: 'Scope fixture' },
    }, onquery: vi.fn() });
    const label = screen.getByText('Scope fixture · fixed scope');
    expect(getComputedStyle(label).position).not.toBe('fixed');
    expect(label.classList.contains('fixed')).toBe(false);
    expect(screen.queryByRole('combobox', { name: 'Filter project' })).toBeNull();
  } finally {
    utilities.remove();
  }
});
