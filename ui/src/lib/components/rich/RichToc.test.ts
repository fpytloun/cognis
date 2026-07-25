import { fireEvent, render, screen, waitFor, within } from '@testing-library/svelte';
import { afterEach, describe, expect, it, vi } from 'vitest';

import RichToc from './RichToc.svelte';

const items = [
  {
    anchor: 'overview',
    requestedAnchor: 'overview',
    label: 'Overview',
    level: 2 as const,
    block: { type: 'section', title: 'Overview' },
  },
  {
    anchor: 'evaluation',
    requestedAnchor: 'evaluation',
    label: 'Evaluation',
    level: 3 as const,
    block: { type: 'section', title: 'Evaluation' },
  },
  {
    anchor: 'edge-cases',
    requestedAnchor: 'edge-cases',
    label: 'Edge cases',
    level: 4 as const,
    block: { type: 'section', title: 'Edge cases' },
  },
];

function stubMobile(matches = true) {
  let listener: ((event: MediaQueryListEvent) => void) | undefined;
  vi.stubGlobal('matchMedia', vi.fn(() => ({
    matches,
    media: '(max-width: 1439.98px)',
    onchange: null,
    addEventListener: (_type: string, value: (event: MediaQueryListEvent) => void) => {
      listener = value;
    },
    removeEventListener: vi.fn(),
    addListener: vi.fn(),
    removeListener: vi.fn(),
    dispatchEvent: vi.fn(),
  })));
  return (next: boolean) => listener?.({ matches: next } as MediaQueryListEvent);
}

afterEach(() => {
  vi.unstubAllGlobals();
  document.body.replaceChildren();
});

describe('RichToc', () => {
  it('renders a compact semantic hierarchy without level badges or cards', () => {
    stubMobile(false);
    const { container } = render(RichToc, {
      items,
      onNavigate: vi.fn(),
    });

    const nav = screen.getByRole('navigation', { name: 'Table of contents' });
    expect(nav.querySelectorAll('ol')).toHaveLength(3);
    expect(nav.querySelector('li[data-level="2"] > ol li[data-level="3"]')).not.toBeNull();
    expect(nav.querySelector('li[data-level="3"] > ol li[data-level="4"]')).not.toBeNull();
    expect(container.querySelector('[class*="badge"], [class*="card"], small')).toBeNull();
    expect(screen.queryByRole('dialog')).toBeNull();
  });

  it('closes the mobile drawer with Escape and restores trigger focus exactly once', async () => {
    stubMobile();
    const trigger = document.createElement('button');
    trigger.setAttribute('aria-label', 'Open table of contents');
    document.body.appendChild(trigger);
    trigger.focus();
    const onClose = vi.fn();

    render(RichToc, {
      items,
      onNavigate: vi.fn(),
      onClose,
      open: true,
    });

    const dialog = await screen.findByRole('dialog', { name: 'Table of contents' });
    await waitFor(() => expect(
      within(dialog).getByRole('button', { name: 'Close table of contents' }),
    ).toHaveFocus());
    await fireEvent.keyDown(dialog, { key: 'Escape' });

    await waitFor(() => expect(onClose).toHaveBeenCalledTimes(1));
    expect(trigger).toHaveFocus();
    expect(screen.queryByRole('dialog', { name: 'Table of contents' })).toBeNull();
  });

  it('closes from the backdrop and after navigation', async () => {
    stubMobile();
    const onClose = vi.fn();
    const onNavigate = vi.fn();
    const mounted = render(RichToc, {
      items,
      onNavigate,
      onClose,
      open: true,
    });

    await fireEvent.click(screen.getByTestId('rich-toc-backdrop'));
    await waitFor(() => expect(onClose).toHaveBeenCalledTimes(1));

    mounted.unmount();
    render(RichToc, {
      items,
      onNavigate,
      onClose,
      open: true,
    });
    await screen.findByRole('dialog', { name: 'Table of contents' });
    await fireEvent.click(screen.getByRole('button', { name: 'Evaluation' }));
    expect(onNavigate).toHaveBeenCalledWith(items[1]);
    await waitFor(() => expect(onClose).toHaveBeenCalledTimes(2));
    expect(screen.queryByRole('dialog', { name: 'Table of contents' })).toBeNull();
  });
});
