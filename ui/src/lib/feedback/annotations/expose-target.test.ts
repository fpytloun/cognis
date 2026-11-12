import { describe, expect, it, vi } from 'vitest';
import { exposeGeometry } from './expose-target';

describe('source-target collision recovery', () => {
  it('scrolls an owning source scroller rather than moving focus out of the composer', () => {
    const scroller = document.createElement('div'), source = document.createElement('p'), textarea = document.createElement('textarea');
    scroller.style.overflowY = 'auto'; scroller.append(source); document.body.append(scroller, textarea);
    Object.defineProperties(scroller, { scrollHeight: { value: 1000 }, clientHeight: { value: 200 } });
    scroller.getBoundingClientRect = () => ({ top: 20, bottom: 220 } as DOMRect);
    let top = 180;
    scroller.scrollBy = vi.fn(options => { top -= (options as ScrollToOptions).top ?? 0; });
    const windowScroll = vi.spyOn(window, 'scrollBy').mockImplementation(() => {});
    textarea.focus();
    exposeGeometry(source, () => ({ space: 'client', rects: [{ left: 10, top, width: 100, height: 40 }] }), { left: 0, top: 20, width: 300, height: 120 });
    expect(scroller.scrollBy).toHaveBeenCalledWith({ top: 80, behavior: 'instant' });
    expect(windowScroll).not.toHaveBeenCalled();
    expect(document.activeElement).toBe(textarea);
    windowScroll.mockRestore(); scroller.remove(); textarea.remove();
  });
  it('uses document scrolling for lower targets and does nothing for an already visible range', () => {
    const source = document.createElement('p'); document.body.append(source);
    let top = 700;
    const scroll = vi.spyOn(window, 'scrollBy').mockImplementation(options => { top -= (options as ScrollToOptions).top ?? 0; });
    const geometry = () => ({ space: 'client' as const, rects: [{ left: 10, top, width: 100, height: 20 }] });
    const visible = { left: 0, top: 8, width: 300, height: 220 };
    exposeGeometry(source, geometry, visible); exposeGeometry(source, geometry, visible);
    expect(scroll).toHaveBeenCalledOnce();
    expect(top).toBe(208);
    scroll.mockRestore(); source.remove();
  });
});
