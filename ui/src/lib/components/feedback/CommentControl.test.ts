import { cleanup, fireEvent, render, screen } from '@testing-library/svelte';
import { afterEach, expect, it, vi } from 'vitest';
import CommentControl from './CommentControl.svelte';

afterEach(cleanup);
it('is one button that opens and closes the comments and names what waits on the reviewer', async () => {
  const onShow = vi.fn();
  const view = render(CommentControl, { onShow, count: 2 });
  const button = screen.getByRole('button', { name: 'Comments, 2 need you' });
  expect(button).toHaveTextContent('2');
  expect(screen.queryByRole('button', { name: 'Comment options' })).toBeNull();
  await fireEvent.click(button);
  expect(onShow).toHaveBeenCalledOnce();
  await view.rerender({ onShow, count: 2, open: true });
  expect(screen.getByRole('button', { name: 'Hide comments' })).toHaveAttribute('aria-pressed', 'true');
});
it('stays disabled when comments are unavailable', () => {
  render(CommentControl, { onShow: vi.fn(), disabled: true });
  expect(screen.getByRole('button', { name: 'Comments' })).toBeDisabled();
});
