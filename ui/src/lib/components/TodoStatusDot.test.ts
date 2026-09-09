import { cleanup, render, screen } from '@testing-library/svelte';
import { readFileSync } from 'node:fs';
import { afterEach, describe, expect, it } from 'vitest';

import TodoStatusDot from './TodoStatusDot.svelte';

afterEach(cleanup);

describe('TodoStatusDot', () => {
  it('animates only in-progress aliases', async () => {
    const view = render(TodoStatusDot, { status: 'in_progress' });
    expect(screen.getByTestId('todo-status-dot')).toHaveAttribute('data-animated', 'true');
    expect(screen.getByTestId('todo-status-dot')).toHaveClass('todo-status-dot--in-progress');

    for (const status of ['pending', 'completed', 'cancelled']) {
      await view.rerender({ status });
      expect(screen.getByTestId('todo-status-dot')).toHaveAttribute('data-animated', 'false');
      expect(screen.getByTestId('todo-status-dot')).not.toHaveClass('todo-status-dot--in-progress');
    }
  });

  it('contains slow reduced-motion-safe animation rules', () => {
    const component = readFileSync(
      `${process.cwd()}/src/lib/components/TodoStatusDot.svelte`,
      'utf8'
    );
    expect(component).toContain('2.8s');
    expect(component).toContain('prefers-reduced-motion: reduce');
    expect(component).toContain('animation: none');
  });
});
