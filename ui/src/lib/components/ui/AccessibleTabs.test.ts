import { fireEvent, render, screen } from '@testing-library/svelte';
import { describe, expect, it, vi } from 'vitest';

import AccessibleTabs from './AccessibleTabs.svelte';

describe('AccessibleTabs', () => {
  it('uses roving tabindex and supports Arrow, Home, and End navigation', async () => {
    const onChange = vi.fn();
    render(AccessibleTabs, {
      tabs: [
        { id: 'one', label: 'One' },
        { id: 'two', label: 'Two' },
        { id: 'three', label: 'Three' },
      ],
      activeId: 'two',
      idPrefix: 'test',
      ariaLabel: 'Example',
      onChange,
    });
    const two = screen.getByRole('tab', { name: 'Two' });
    expect(two).toHaveAttribute('tabindex', '0');
    await fireEvent.keyDown(two, { key: 'End' });
    expect(onChange).toHaveBeenCalledWith('three');
    await fireEvent.keyDown(two, { key: 'Home' });
    expect(onChange).toHaveBeenCalledWith('one');
    await fireEvent.keyDown(two, { key: 'ArrowRight' });
    expect(onChange).toHaveBeenCalledWith('three');
  });

  it('can disable the right edge fade when controls share the header', () => {
    render(AccessibleTabs, {
      tabs: [{ id: 'one', label: 'One' }],
      activeId: 'one',
      idPrefix: 'test',
      ariaLabel: 'Example',
      edgeFade: false,
      onChange: vi.fn(),
    });
    expect(screen.queryByTestId('tabs-edge-fade')).toBeNull();
  });
});
