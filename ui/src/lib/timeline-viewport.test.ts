import { describe, expect, it, vi } from 'vitest';
import { observeTimelineResizeAutoScroll } from './timeline-viewport';

class ResizeObserverMock {
  static instances: ResizeObserverMock[] = [];

  observe = vi.fn();
  disconnect = vi.fn();
  callback: ResizeObserverCallback;

  constructor(callback: ResizeObserverCallback) {
    this.callback = callback;
    ResizeObserverMock.instances.push(this);
  }
}

describe('observeTimelineResizeAutoScroll', () => {
  it('observes content and viewport resizes by default', () => {
    ResizeObserverMock.instances = [];
    const content = document.createElement('div');
    const viewport = document.createElement('div');

    const cleanup = observeTimelineResizeAutoScroll({
      autoScrollOnResize: true,
      contentElement: content,
      viewportElement: viewport,
      scrollToBottom: vi.fn(),
      resizeObserver: ResizeObserverMock as unknown as typeof ResizeObserver,
      requestAnimationFrame: vi.fn(),
    });

    expect(cleanup).toEqual(expect.any(Function));
    expect(ResizeObserverMock.instances).toHaveLength(1);
    expect(ResizeObserverMock.instances[0]?.observe).toHaveBeenCalledWith(content);
    expect(ResizeObserverMock.instances[0]?.observe).toHaveBeenCalledWith(viewport);
  });

  it('skips observer creation when parent owns chat scrolling', () => {
    ResizeObserverMock.instances = [];

    const cleanup = observeTimelineResizeAutoScroll({
      autoScrollOnResize: false,
      contentElement: document.createElement('div'),
      viewportElement: document.createElement('div'),
      scrollToBottom: vi.fn(),
      resizeObserver: ResizeObserverMock as unknown as typeof ResizeObserver,
      requestAnimationFrame: vi.fn(),
    });

    expect(cleanup).toBeNull();
    expect(ResizeObserverMock.instances).toHaveLength(0);
  });

  it('schedules a bounded scroll-to-bottom burst through requestAnimationFrame on resize', () => {
    ResizeObserverMock.instances = [];
    const scrollToBottom = vi.fn();
    const requestAnimationFrame = vi.fn((callback: FrameRequestCallback) => {
      callback(0);
      return 1;
    });

    observeTimelineResizeAutoScroll({
      autoScrollOnResize: true,
      contentElement: document.createElement('div'),
      viewportElement: null,
      scrollToBottom,
      resizeObserver: ResizeObserverMock as unknown as typeof ResizeObserver,
      requestAnimationFrame,
    });

    ResizeObserverMock.instances[0]?.callback([], ResizeObserverMock.instances[0] as unknown as ResizeObserver);

    expect(requestAnimationFrame).toHaveBeenCalledTimes(4);
    expect(scrollToBottom).toHaveBeenCalledTimes(4);
  });
});
