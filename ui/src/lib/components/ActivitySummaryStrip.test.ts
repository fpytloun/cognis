import { fireEvent, render, screen } from '@testing-library/svelte';
import { describe, expect, it, vi } from 'vitest';

import ActivitySummaryStrip from './ActivitySummaryStrip.svelte';

const metrics = [
  { id: 'files', label: 'Files', value: 3, additions: 12, deletions: 4 },
  { id: 'commands', label: 'Commands', value: 7 }
];

describe('ActivitySummaryStrip', () => {
  it('renders noninteractive metrics when no selection callback exists', () => {
    render(ActivitySummaryStrip, { metrics });

    expect(screen.getByTestId('activity-summary-strip')).toBeInTheDocument();
    expect(screen.queryAllByRole('button')).toHaveLength(0);
    expect(screen.getByText('Commands')).toBeInTheDocument();
  });

  it('renders actionable metrics when selection is supported', async () => {
    const onSelect = vi.fn();
    render(ActivitySummaryStrip, { metrics, onSelect });

    const buttons = screen.getAllByRole('button');
    expect(buttons).toHaveLength(2);
    await fireEvent.click(buttons[1]);
    expect(onSelect).toHaveBeenCalledWith(metrics[1]);
  });
});
