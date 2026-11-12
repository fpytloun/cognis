import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import { afterEach, expect, it, vi } from 'vitest';
import DocumentFindBar from './DocumentFindBar.svelte';

afterEach(() => { cleanup(); vi.unstubAllGlobals(); document.body.replaceChildren(); });

it('reports adapter failures separately from the compact counter and can still close', async () => {
  let closed = false;
  render(DocumentFindBar, { content: undefined, adapter: {
    search: async () => { throw new Error('PDF worker unavailable'); }, select: async () => {}, clear() {},
  }, onclose: () => { closed = true; } });
  await fireEvent.input(screen.getByRole('searchbox'), { target: { value: 'review' } });
  expect(await screen.findByRole('alert')).toHaveTextContent('Search failed');
  expect(screen.getByRole('alert')).toHaveAttribute('title', 'Could not search this document. Try again.');
  await fireEvent.click(screen.getByRole('button', { name: 'Close search' }));
  expect(closed).toBe(true);
});

it('searches rendered document text, navigates and clears highlights on dismissal', async () => {
  const highlights = new Map<string, Set<Range>>();
  vi.stubGlobal('CSS', { highlights });
  vi.stubGlobal('Highlight', class extends Set<Range> { constructor(...ranges: Range[]) { super(ranges); } });
  const content = document.createElement('article');
  content.innerHTML = '<p>Ready for review. Ready for launch.</p>';
  document.body.append(content);
  let closed = false;
  const view = render(DocumentFindBar, { content, onclose: () => { closed = true; } });
  const field = screen.getByRole('searchbox', { name: 'Find in document' });
  await fireEvent.input(field, { target: { value: 'ready' } });
  await waitFor(() => expect(screen.getByRole('status')).toHaveTextContent('1 of 2'));
  await fireEvent.submit(screen.getByRole('search'));
  await waitFor(() => expect(screen.getByRole('status')).toHaveTextContent('2 of 2'));
  await fireEvent.keyDown(field, { key: 'Enter', shiftKey: true });
  await waitFor(() => expect(screen.getByRole('status')).toHaveTextContent('1 of 2'));
  await fireEvent.keyDown(field, { key: 'Escape' });
  expect(closed).toBe(true);
  view.unmount();
  expect(highlights.size).toBe(0);
  expect(content.textContent).toBe('Ready for review. Ready for launch.');
});
