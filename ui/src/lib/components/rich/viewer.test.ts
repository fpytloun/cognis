import { fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import RichViewerChrome from './RichViewerChrome.svelte';
import RichDeliverable from './RichDeliverable.svelte';
import ViewerIcon from './ViewerIcon.svelte';
import { heroBadges } from './hero-badges';
import { setViewerTheme } from './viewer-theme';

describe('viewer chrome', () => {
  it('uses the same square-and-up-arrow share glyph as native sharing controls', () => {
    const { container } = render(ViewerIcon, { name: 'share' });
    const icon = container.querySelector('[data-viewer-icon="share"]');
    expect(icon).toHaveClass('lucide-share');
    expect(icon).toHaveAttribute('aria-hidden', 'true');
    expect(icon).not.toHaveClass('lucide-link', 'lucide-external-link', 'lucide-share-2');
  });
  beforeEach(() => setViewerTheme('system'));
  afterEach(() => {
    document.documentElement.removeAttribute('data-resolved-theme');
    document.documentElement.removeAttribute('data-theme-family');
  });
  it('omits theme controls from in-app viewer chrome', () => {
    render(RichViewerChrome, { surface: 'embedded', onCopy: vi.fn() });
    expect(screen.queryByRole('button', { name: /^Theme:/ })).toBeNull();
    expect(screen.getByRole('button', { name: 'Copy document' })).toBeTruthy();
  });
  it('keeps embedded and fullscreen documents on live app appearance despite saved reader overrides', async () => {
    setViewerTheme('light');
    document.documentElement.dataset.resolvedTheme = 'dark';
    document.documentElement.dataset.themeFamily = 'paper';
    render(RichDeliverable, {
      surface: 'embedded', title: 'App report',
      payload: { blocks: [{ type: 'markdown', content: 'Report body' }] },
    });
    const root = screen.getByTestId('rich-deliverable');
    await waitFor(() => expect(root).toHaveAttribute('data-viewer-theme', 'dark'));
    expect(root).toHaveAttribute('data-theme-family', 'paper');
    await fireEvent.click(screen.getByRole('button', { name: 'Open fullscreen' }));
    const full = document.querySelector('.rich-full');
    expect(full).toHaveAttribute('data-viewer-theme', 'dark');
    expect(screen.queryByRole('button', { name: /^Theme:/ })).toBeNull();
    document.documentElement.dataset.resolvedTheme = 'light';
    setViewerTheme('dark');
    await waitFor(() => expect(root).toHaveAttribute('data-viewer-theme', 'light'));
    expect(full).toHaveAttribute('data-viewer-theme', 'light');
    await fireEvent.click(screen.getByRole('button', { name: 'Close' }));
  });
  it.each([undefined, {}, { icon: { name: 'not-registered', alt: 'Unknown' } }])('omits absent identity %j', (identity) => {
    render(RichViewerChrome, { identity, onCopy: vi.fn() });
    expect(screen.queryByTestId('rich-viewer-identity')).toBeNull();
  });
  it.each([
    { label: 'Engineering' },
    { icon: { name: 'activity', alt: 'Engineering' } },
    { label: 'Engineering', icon: { name: 'activity' } },
  ])('renders authored identity %j', (identity) => {
    render(RichViewerChrome, { identity, onCopy: vi.fn() });
    expect(screen.getByTestId('rich-viewer-identity')).toBeTruthy();
  });
  it('preserves long title and persists an explicit theme', async () => {
    const title = 'A long document title '.repeat(40);
    render(RichViewerChrome, { title, onCopy: vi.fn() });
    expect(screen.getByTitle(title.trim())).toHaveTextContent(title.trim());
    await fireEvent.click(screen.getByRole('button', { name: 'Theme: system. Switch to light theme' }));
    expect(localStorage.getItem('cognis-rich-viewer-theme')).toBe('light');
  });
  it('uses trusted SVG controls in reference order and exposes selection', async () => {
    const { container } = render(RichViewerChrome, { title: 'Report', onCopy: vi.fn(), pdfUrl: '/report.pdf', htmlUrl: '/report.html', markdownUrl: '/report.md' });
    await screen.findByTitle('Download document');
    expect(Array.from(container.querySelectorAll('nav[aria-label="Viewer controls"] button, nav[aria-label="Viewer controls"] summary')).map(el => el.getAttribute('aria-label')))
      .toEqual(['Theme: system. Switch to light theme', 'Download document', 'Copy document']);
    expect(screen.getByTitle('Download document').querySelector('svg')).toHaveClass('lucide-download');
    expect(container.querySelector('select')).toBeNull();
    const theme = container.querySelector('.theme-control') as HTMLButtonElement;
    expect(theme).toHaveAttribute('data-theme-mode', 'system');
    await fireEvent.click(theme);
    expect(theme).toHaveAttribute('data-theme-mode', 'light');
    expect(theme.querySelector('svg')).toHaveAttribute('data-viewer-icon', 'light');
    await fireEvent.click(theme);
    expect(theme).toHaveAttribute('data-theme-mode', 'dark');
    expect(theme.querySelector('svg')).toHaveAttribute('data-viewer-icon', 'dark');
    await fireEvent.click(theme);
    expect(theme).toHaveAttribute('data-theme-mode', 'system');
    expect(theme.querySelector('svg')).toHaveAttribute('data-viewer-icon', 'system');
    await fireEvent.click(screen.getByTitle('Download document'));
    expect(screen.getByRole('link', { name: 'PDF' })).toHaveAttribute('href', '/report.pdf');
    expect(screen.getByRole('link', { name: 'Single HTML' })).toHaveAttribute('href', '/report.html');
    expect(screen.getByRole('link', { name: 'Markdown' })).toHaveAttribute('href', '/report.md');
    await fireEvent.keyDown(screen.getByTitle('Download document'), { key: 'Escape' });
    expect(container.querySelector('.download-menu')).not.toHaveAttribute('open');
  });
  it('normalizes typed badges without rendering unknown objects', () => {
    expect(heroBadges([{ label: 'Approved', tone: 'success' }, { label: 'Pending', tone: 'unknown' }, {}, 'Legacy']))
      .toEqual([{ label: 'Approved', tone: 'success' }, { label: 'Pending', tone: 'neutral' }, { label: 'Legacy', tone: 'neutral' }]);
  });
  it('reports clipboard rejection and renders summary KV', async () => {
    Object.defineProperty(navigator, 'clipboard', {
      configurable: true, value: { writeText: vi.fn().mockRejectedValue(new Error('denied')) },
    });
    const { container } = render(RichDeliverable, {
      surface: 'standalone',
      payload: { blocks: [
        { type: 'hero', title: 'Report', badges: [{ label: 'Approved', tone: 'success' }] },
        { type: 'kv', variant: 'summary', items: [{ label: 'Source', value: 'Verified' }] },
      ] },
    });
    expect(container.querySelector('.rich-kv-summary')).toHaveTextContent('Verified');
    expect(container.querySelector('.rich-hero-badges .tone-success')).toHaveTextContent('Approved');
    await fireEvent.click(screen.getByRole('button', { name: 'Copy document' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('Could not copy');
    expect(screen.queryByRole('button', { name: 'Copied' })).toBeNull();
  });
});
