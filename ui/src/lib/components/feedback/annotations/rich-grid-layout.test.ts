import { render } from '@testing-library/svelte';
import { describe, expect, it } from 'vitest';
import RichGridLayoutHarness from './RichGridLayoutHarness.svelte';

describe('annotation publication anchors preserve rich grid stretch structure', () => {
  it.each(['unbound', 'view', 'comment'] as const)('keeps cards directly in their one canonical anchor in %s mode', mode => {
    const { container } = render(RichGridLayoutHarness, { mode });
    for (const id of ['short-card', 'long-card']) {
      const anchor = container.querySelector<HTMLElement>(`[data-feedback-rich-block="${id}"]`)!;
      expect(anchor).toHaveClass('rich-block-anchor');
      expect(anchor.parentElement).toHaveClass('rich-block-list');
      expect(anchor.firstElementChild).toHaveClass('rich-card');
      expect(anchor.querySelectorAll('.rich-card')).toHaveLength(1);
      expect(anchor.id).not.toBe('');
      expect(anchor.style.gridColumn).toBe('span 1');
      expect(anchor.querySelector('summary')).toBeNull();
    }
  });
});
