import { describe, expect, it } from 'vitest';
import { feedbackOverlayBounds } from './overlay-bounds';

describe('shared feedback usable viewport', () => {
  const visual = { left: 0, top: 0, right: 390, bottom: 844 };
  const shell = { left: 12, top: 56, right: 378, bottom: 776 };
  it('respects app header, mobile navigation and safe-area bounds', () => {
    expect(feedbackOverlayBounds(shell, visual)).toEqual({ ...shell, width: 366, height: 720 });
  });
  it('intersects keyboard occlusion without subtracting mobile navigation twice', () => {
    expect(feedbackOverlayBounds(shell, { ...visual, bottom: 430 })).toMatchObject({ top: 56, bottom: 422, height: 366 });
  });
  it('does not invent a mobile-navigation inset for a standalone public surface', () => {
    expect(feedbackOverlayBounds({ ...visual, left: 12, top: 12, right: 378, bottom: 832 }, visual))
      .toMatchObject({ bottom: 832, height: 820 });
  });
  it('tracks visual viewport panning and zoom offsets', () => {
    expect(feedbackOverlayBounds(shell, { left: 20, top: 100, right: 360, bottom: 500 }))
      .toMatchObject({ left: 28, top: 108, right: 352, bottom: 492 });
  });
});
