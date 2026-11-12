import { cleanup, render, screen } from '@testing-library/svelte';
import { afterEach, describe, expect, it } from 'vitest';

import ActivityLabel from './ActivityLabel.svelte';

afterEach(cleanup);

describe('ActivityLabel', () => {
  it('reads as ongoing only while the activity runs', async () => {
    const view = render(ActivityLabel, { label: 'Running commands…', active: true });
    expect(screen.getByText('Running commands…')).toBeTruthy();

    await view.rerender({ label: 'Running commands…', active: false });
    expect(screen.getByText('Ran commands')).toBeTruthy();
    expect(screen.queryByText('Running commands…')).toBeNull();
  });
});
