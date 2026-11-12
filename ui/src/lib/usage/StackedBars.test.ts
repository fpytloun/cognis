import { cleanup, fireEvent, render, screen } from '@testing-library/svelte';
import { afterEach, describe, expect, it, vi } from 'vitest';
import StackedBars from './StackedBars.svelte';
import { niceMax, type BarBucket } from './bars';

afterEach(cleanup);
const point = { key: 'd1', label: 'd1', value: 2, display: '2', patch: {}, metric: 'm', detailAvailable: true, explanation: '', population: '' };
function bucket(key: string, values: (number | null)[], partial = false): BarBucket {
  return { key, label: key, title: `Day ${key}`, partial, display: values.every((value) => value === null) ? '—' : String(values.reduce<number>((a, b) => a + (b ?? 0), 0)),
    point: key === 'd1' ? point : null,
    segments: values.map((value, index) => ({ key: `s${index}`, label: `Part ${index}`, value, color: 'black' })) };
}
describe('stacked daily bars', () => {
  it('uses round axis maxima', () => {
    expect([niceMax(0), niceMax(0.7), niceMax(3), niceMax(17), niceMax(240)]).toEqual([1, 1, 5, 20, 250]);
  });
  it('labels every bucket, keeps unknown days distinct from zero and drills on select', async () => {
    const onpoint = vi.fn();
    const { container } = render(StackedBars, { label: 'Tokens', format: String, onpoint, buckets: [bucket('d1', [1, 1]), bucket('d2', [null, null]), bucket('d3', [0, 0], true)] });
    expect(screen.getByRole('button', { name: /Day d1: 2\. Part 0 1, Part 1 1/ })).toBeTruthy();
    expect(screen.getByRole('button', { name: /Day d2: —/ })).toBeTruthy();
    expect(screen.getByRole('button', { name: /Day d3: 0 \(partial day\)/ })).toBeTruthy();
    expect(container.querySelectorAll('rect.missing')).toHaveLength(1);
    await fireEvent.click(screen.getByRole('button', { name: /Day d1/ }));
    expect(onpoint).toHaveBeenCalledWith(point);
  });
  it('shows an explicit empty state when nothing is known', () => {
    render(StackedBars, { label: 'Cost', format: String, buckets: [bucket('d1', [null])] });
    expect(screen.getByText('No data for this range')).toBeTruthy();
  });
});
