import { cleanup, fireEvent, render, screen } from '@testing-library/svelte';
import { afterEach, describe, expect, it, vi } from 'vitest';
import RankedCost from './RankedCost.svelte';
import { compositionBand } from './composition.test-data';

afterEach(cleanup);
describe('ranked cost breakdown', () => {
  it('ranks returned groups with rounded amounts, shares and drills', async () => {
    const oncohort = vi.fn();
    render(RankedCost, { title: 'User', dimension: 'user', band: compositionBand(), oncohort, detailAvailable: true });
    const known = screen.getByRole('button', { name: /user Alpha: \$7\.50, 75%. Inspect filtered requests/ });
    expect(known.querySelector('[title="$7.50"]')?.textContent).toBe('$7.50');
    await fireEvent.click(known);
    expect(oncohort).toHaveBeenCalledWith(expect.objectContaining({ amount: '7.500000000000000001', drill: { filters: { user: 'synthetic-user-alpha' } } }));
    // Unknown money is never a zero-valued share; a real zero stays visible.
    expect(screen.getByRole('button', { name: /unpriced only: Estimate unavailable, Unavailable/ })).toBeTruthy();
    expect(screen.getByRole('button', { name: /real zero: \$0\.00, 0%/ })).toBeTruthy();
  });
  it('keeps redacted cohorts without inventing an identity filter', async () => {
    const oncohort = vi.fn();
    render(RankedCost, { title: 'User', dimension: 'user', band: compositionBand(), oncohort, detailAvailable: true });
    await fireEvent.click(screen.getByRole('button', { name: /Unallocated \/ redacted:.*Explain unavailable detail/ }));
    expect(oncohort).toHaveBeenCalledWith(expect.objectContaining({ label: 'Unallocated / redacted', drill: null }));
  });
  it('shows a missing breakdown explicitly', () => {
    render(RankedCost, { title: 'Agent', dimension: 'agent', oncohort: vi.fn() });
    expect(screen.getByText('No breakdown for this range')).toBeTruthy();
  });
  it('preserves tiny nonzero amounts', () => {
    const band = compositionBand();
    band.amount = '0.000000000001';
    band.dimensions.user = { state: 'ready', complete: true, groups: [{ key: 'tiny', label: 'Tiny', amount: '0.000000000001', drill: null }] };
    render(RankedCost, { title: 'User', dimension: 'user', band, oncohort: vi.fn() });
    expect(screen.getByText('<$0.01')).toBeTruthy();
    expect(screen.getByRole('button', { name: /Tiny: <\$0\.01/ })).toBeTruthy();
  });
  it('limits long lists and reveals every returned group on demand', async () => {
    const band = compositionBand();
    band.amount = '8';
    band.dimensions.model = { state: 'ready', complete: true, groups: Array.from({ length: 8 }, (_, i) => ({ key: String(i), label: `Model ${i}`, amount: '1', drill: null })) };
    render(RankedCost, { title: 'Model', dimension: 'model', band, oncohort: vi.fn() });
    expect(screen.getAllByRole('button', { name: /^Model \d/ })).toHaveLength(6);
    await fireEvent.click(screen.getByRole('button', { name: 'Show all 8' }));
    expect(screen.getAllByRole('button', { name: /^Model \d/ })).toHaveLength(8);
  });
});
