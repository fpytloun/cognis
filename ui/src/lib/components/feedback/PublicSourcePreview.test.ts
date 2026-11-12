import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import PublicSourcePreview from './PublicSourcePreview.svelte';
import { viewerTheme } from '../rich/viewer-theme';

beforeEach(() => {
  vi.stubGlobal('matchMedia', vi.fn(() => ({
    matches: true, addEventListener: vi.fn(), removeEventListener: vi.fn(),
  })));
  localStorage.clear();
  viewerTheme.set(null);
  document.documentElement.dataset.themeFamily = 'classic';
  document.documentElement.dataset.resolvedTheme = 'light';
});
afterEach(() => {
  cleanup(); vi.unstubAllGlobals();
  delete document.documentElement.dataset.themeFamily;
  delete document.documentElement.dataset.resolvedTheme;
});

it.each(['video/mp4', 'audio/mpeg'])('renders native controls for public %s bytes', async mime_type => {
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response('', { status: 401 })));
  const { container } = render(PublicSourcePreview, {
    content: {
      format: 'binary', mime_type, filename: mime_type.startsWith('video') ? 'Review.mp4' : 'Review.mp3',
      content: null, rich_payload: null, bytes_path: '/api/v1/public/feedback/source/bytes',
    },
    mediaUrl: 'blob:public-media',
    guard: { signal: new AbortController().signal, assertCurrent() {}, validate: async () => {} },
  });
  const media = container.querySelector(mime_type.startsWith('video') ? 'video' : 'audio');
  expect(media).toHaveAttribute('src', 'blob:public-media');
  expect(media).toHaveAttribute('controls');
  expect(container.querySelector('pre')).toBeNull();
});

describe('comment-enabled public appearance', () => {
  it.each(['rich', 'markdown'])('uses only the owner family for anonymous %s viewers', async format => {
    const fetcher = vi.fn().mockResolvedValue(new Response('', { status: 401 }));
    vi.stubGlobal('fetch', fetcher);
    const { container } = render(PublicSourcePreview, {
      content: {
        format, mime_type: format === 'rich' ? 'application/json' : 'text/plain', filename: 'Review', content: 'Shared paragraph.',
        rich_payload: format === 'rich' ? { blocks: [{ type: 'markdown', content: 'Shared paragraph.' }] } : null,
        bytes_path: null, theme_family: 'gruvbox',
      }, mediaUrl: null,
      guard: { signal: new AbortController().signal, assertCurrent() {}, validate: async () => {} },
    });
    const palette = container.querySelector('[data-theme-family]')!;
    await waitFor(() => expect(palette).toHaveAttribute('data-theme-family', 'gruvbox'));
    expect(palette).toHaveAttribute('data-resolved-theme', 'dark');
    await waitFor(() => expect(container.querySelector('.rich-deliverable')).toHaveAttribute('data-theme-family', 'gruvbox'));
    expect(container.querySelector('.rich-deliverable')).toHaveAttribute('data-viewer-theme', 'dark');
    expect(fetcher).toHaveBeenCalledWith('/api/v1/user-preferences',
      expect.objectContaining({ credentials: 'same-origin', cache: 'no-store' }));
  });

  it('lets the signed-in reader override both owner family and device appearance', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(Response.json({
      display: { theme: 'light', theme_family: 'paper' },
    })));
    const { container } = render(PublicSourcePreview, {
      content: {
        format: 'rich', mime_type: 'application/json', filename: 'Review', content: '',
        rich_payload: { blocks: [{ type: 'markdown', content: 'Shared paragraph.' }] },
        bytes_path: null, theme_family: 'gruvbox',
      }, mediaUrl: null,
      guard: { signal: new AbortController().signal, assertCurrent() {}, validate: async () => {} },
    });
    await waitFor(() => expect(container.querySelector('.rich-deliverable')).toHaveAttribute('data-theme-family', 'paper'));
    expect(container.querySelector('.rich-deliverable')).toHaveAttribute('data-viewer-theme', 'light');
    await fireEvent.click(screen.getByRole('button', { name: /Switch to light theme/ }));
    await fireEvent.click(screen.getByRole('button', { name: /Switch to dark theme/ }));
    expect(container.querySelector('.rich-deliverable')).toHaveAttribute('data-viewer-theme', 'dark');
    await waitFor(() => expect(document.querySelector('meta[name="color-scheme"]')).toHaveAttribute('content', 'dark'));
  });
});
