import { fireEvent, render, screen } from '@testing-library/svelte';
import { describe, expect, it, vi } from 'vitest';
import FileDiffViewer from './FileDiffViewer.svelte';

describe('FileDiffViewer', () => {
  it('keeps a controlled selected diff open and exposes expand', async () => {
    const onExpand = vi.fn();
    render(FileDiffViewer, {
      diffs: [{ path: 'src/file.ts', diff: '@@ -1 +1 @@\n-old\n+new' }],
      collapsible: false,
      onExpand,
    });
    expect(screen.getByText('Expand diff')).toBeTruthy();
    expect(screen.getByText('Expand diff')).toHaveClass('hidden');
    expect(screen.getByText('Expand diff')).toHaveClass('sm:inline-flex');
    expect(screen.getByText('old')).toBeTruthy();
    await fireEvent.click(screen.getByRole('button'));
    expect(onExpand).toHaveBeenCalledOnce();
    expect(screen.getByText('old')).toBeTruthy();
  });

  it('never displays truncation badges but retains omitted rows', () => {
    render(FileDiffViewer, {
      diffs: [{
        path: '',
        diff: '',
        omitted_count: 12,
      }],
    });
    expect(screen.queryByText(/Truncated/i)).toBeNull();
    expect(screen.getByText(/12 additional file diffs omitted/)).toBeTruthy();
  });
});
