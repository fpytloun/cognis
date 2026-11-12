import { render, screen } from '@testing-library/svelte';
import { describe, expect, it } from 'vitest';
import DownloadMenu from './DownloadMenu.svelte';

describe('DownloadMenu', () => {
  it('marks every format as a download instead of an external navigation', () => {
    render(DownloadMenu, { pdfUrl: '/download.pdf', htmlUrl: '/download.html', markdownUrl: '/download.md' });
    for (const name of ['PDF', 'Single HTML', 'Markdown']) {
      const link = screen.getByRole('link', { name, hidden: true });
      expect(link).toHaveAttribute('download');
      expect(link).not.toHaveAttribute('target');
    }
  });
});
