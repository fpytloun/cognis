import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import { afterEach, expect, it } from 'vitest';
import FindBar from './FindBar.svelte';

afterEach(() => { cleanup(); document.body.replaceChildren(); });

it('uses the owning toolbar and restores the header on close', async () => {
  const reader = document.createElement('section');
  reader.dataset.findContainer = '';
  reader.innerHTML = '<header data-find-toolbar>Outer reader</header><section data-find-container><header data-find-toolbar><h2>Document</h2><nav data-find-actions>Actions</nav></header><main></main></section>';
  document.body.append(reader);
  const header = reader.querySelector('section')!.querySelector<HTMLElement>('header')!;
  const view = render(FindBar, { target: reader.querySelector('main')!, props: {
    query: '', label: 'Find in document', count: '12 of 45',
    onSubmit() {}, onNext() {}, onPrevious() {}, onClose() {},
  } });
  await waitFor(() => expect(header).toContainElement(screen.getByRole('search')));
  expect(reader.querySelector('header')).not.toHaveAttribute('data-find-open');
  expect(screen.getByRole('searchbox')).toHaveAttribute('type', 'text');
  expect(screen.getByRole('status')).toHaveTextContent('12 of 45');
  view.unmount();
  expect(header).not.toHaveAttribute('data-find-open');
  expect(header).toHaveTextContent('DocumentActions');
  expect(header.style.height).toBe('');
});

it('offers labelled search, status, keyboard navigation and dismissal', async () => {
  let action = '';
  render(FindBar, {
    query: 'ready', label: 'Find in document', count: '1 of 2',
    onSubmit: () => { action = 'next'; }, onNext: () => { action = 'next'; },
    onPrevious: () => { action = 'previous'; }, onClose: () => { action = 'close'; },
  });
  const field = screen.getByRole('searchbox', { name: 'Find in document' });
  expect(field).toHaveValue('ready');
  expect(screen.getByRole('status')).toHaveTextContent('1 of 2');
  await fireEvent.keyDown(field, { key: 'Enter', shiftKey: true });
  expect(action).toBe('previous');
  await fireEvent.submit(screen.getByRole('search'));
  expect(action).toBe('next');
  await fireEvent.keyDown(field, { key: 'Escape' });
  expect(action).toBe('close');
});

it('disables navigation while matching is unavailable but permits dismissal', () => {
  render(FindBar, {
    query: '', label: 'Search this conversation', count: 'Searching…', navigationDisabled: true,
    onSubmit() {}, onNext() {}, onPrevious() {}, onClose() {},
  });
  expect(screen.getByRole('button', { name: 'Previous match' })).toBeDisabled();
  expect(screen.getByRole('button', { name: 'Next match' })).toBeDisabled();
  expect(screen.getByRole('button', { name: 'Close search' })).toBeEnabled();
});
