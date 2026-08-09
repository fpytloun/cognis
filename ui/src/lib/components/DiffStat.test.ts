import { render, screen } from '@testing-library/svelte';
import { describe, expect, it } from 'vitest';
import DiffStat from './DiffStat.svelte';

describe('DiffStat', () => {
  it('renders explicit file text and colored nonzero additions and deletions', () => {
    render(DiffStat, { files: 2, additions: 5, deletions: 3 });
    expect(screen.getByText('2 files')).toBeTruthy();
    expect(screen.getByLabelText('5 additions')).toHaveClass('text-emerald-300');
    expect(screen.getByLabelText('3 deletions')).toHaveClass('text-rose-300');
    expect(screen.queryByText(/2F|5C/)).toBeNull();
  });

  it('omits zero-only fragments', () => {
    render(DiffStat, { files: 0, additions: 0, deletions: 0, compact: true });
    expect(screen.getByText('0 files')).toBeTruthy();
    expect(screen.queryByLabelText(/additions|deletions/)).toBeNull();
  });
});
