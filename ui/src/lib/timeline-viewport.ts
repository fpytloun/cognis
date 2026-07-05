type ResizeObserverConstructor = typeof ResizeObserver;

export type TimelineResizeAutoScrollOptions = {
  autoScrollOnResize: boolean;
  contentElement: Element | null;
  viewportElement: Element | null;
  scrollToBottom: () => void;
  resizeObserver?: ResizeObserverConstructor;
  requestAnimationFrame?: (callback: FrameRequestCallback) => number;
};

export function observeTimelineResizeAutoScroll({
  autoScrollOnResize,
  contentElement,
  viewportElement,
  scrollToBottom,
  resizeObserver = globalThis.ResizeObserver,
  requestAnimationFrame = globalThis.requestAnimationFrame,
}: TimelineResizeAutoScrollOptions): (() => void) | null {
  if (!autoScrollOnResize || (!contentElement && !viewportElement) || typeof resizeObserver === 'undefined') {
    return null;
  }

  const scheduleFrame = requestAnimationFrame ?? ((callback: FrameRequestCallback) => {
    callback(0);
    return 0;
  });
  let pending = false;
  let disposed = false;
  const burstFrames = 4;

  function scheduleScroll(remainingFrames: number): void {
    scheduleFrame(() => {
      if (disposed) return;
      scrollToBottom();
      if (remainingFrames > 1) {
        scheduleScroll(remainingFrames - 1);
        return;
      }
      pending = false;
    });
  }

  const observer = new resizeObserver(() => {
    if (pending) return;
    pending = true;
    scheduleScroll(burstFrames);
  });

  if (contentElement) observer.observe(contentElement);
  if (viewportElement) observer.observe(viewportElement);

  return () => {
    disposed = true;
    observer.disconnect();
  };
}
