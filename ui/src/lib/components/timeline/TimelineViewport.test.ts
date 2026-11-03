import { fireEvent, render, screen } from '@testing-library/svelte';
import { beforeAll, describe, expect, it, vi } from 'vitest';

import TimelineViewportFixture from './TimelineViewport.test-fixture.svelte';
import TimelineViewportJumpFixture from './TimelineViewport.jump-test-fixture.svelte';

describe('TimelineViewport user scroll intent', () => {
  beforeAll(() => {
    vi.stubGlobal('ResizeObserver', class {
      observe() {}
      disconnect() {}
    });
    vi.stubGlobal('requestAnimationFrame', (callback: FrameRequestCallback) => {
      callback(0);
      return 1;
    });
  });

  it('consumes synthetic user-scroll intent once and ignores later reflow scrolls', async () => {
    render(TimelineViewportFixture);
    const viewport = screen.getByTestId('timeline-intent-viewport');
    Object.defineProperties(viewport, {
      scrollHeight: { configurable: true, value: 1000 },
      clientHeight: { configurable: true, value: 200 },
    });

    viewport.scrollTop = 500;
    await fireEvent.scroll(viewport);
    await fireEvent.wheel(viewport, { deltaY: -120 });
    viewport.scrollTop = 400;
    await fireEvent.scroll(viewport);
    expect(screen.getByTestId('timeline-intent-state')).toHaveTextContent('paused');

    viewport.scrollTop = 800;
    await fireEvent.scroll(viewport);
    expect(screen.getByTestId('timeline-intent-state')).toHaveTextContent('paused');
  });

  it('does not arm scroll intent for unrelated keyboard or pointer input', async () => {
    render(TimelineViewportFixture);
    const viewport = screen.getByTestId('timeline-intent-viewport');
    Object.defineProperties(viewport, {
      scrollHeight: { configurable: true, value: 1000 },
      clientHeight: { configurable: true, value: 200 },
    });

    viewport.scrollTop = 500;
    await fireEvent.keyDown(viewport, { key: 'Enter' });
    await fireEvent.pointerDown(viewport);
    viewport.scrollTop = 400;
    await fireEvent.scroll(viewport);

    expect(screen.getByTestId('timeline-intent-state')).toHaveTextContent('following');
  });

  it.each([
    ['touch', async (viewport: HTMLElement) => fireEvent.touchStart(viewport)],
    ['scrolling key', async (viewport: HTMLElement) => fireEvent.keyDown(viewport, { key: 'PageUp' })],
  ])('consumes %s intent on only the next synthetic scroll', async (_label, armIntent) => {
    render(TimelineViewportFixture);
    const viewport = screen.getByTestId('timeline-intent-viewport');
    Object.defineProperties(viewport, {
      scrollHeight: { configurable: true, value: 1000 },
      clientHeight: { configurable: true, value: 200 },
    });

    viewport.scrollTop = 500;
    await fireEvent.scroll(viewport);
    await armIntent(viewport);
    viewport.scrollTop = 400;
    await fireEvent.scroll(viewport);
    expect(screen.getByTestId('timeline-intent-state')).toHaveTextContent('paused');

    viewport.scrollTop = 800;
    await fireEvent.scroll(viewport);
    expect(screen.getByTestId('timeline-intent-state')).toHaveTextContent('paused');
  });

  it('jumps to the visible message start closest to the reading probe with a single scroll write', async () => {
    const requests: Array<{ scrollTop: number; rowKey: string | null }> = [];
    render(TimelineViewportJumpFixture, {
      onJumpToActiveStart: (request: { scrollTop: number; rowKey: string | null }) => {
        requests.push(request);
        viewport.scrollTop = request.scrollTop;
      },
    });
    const viewport = screen.getByTestId('timeline-jump-viewport');
    const rect = (top: number, height: number) => ({
      top, bottom: top + height, height, left: 0, right: 300, width: 300, x: 0, y: top, toJSON: () => ({}),
    }) as DOMRect;
    // Viewport 0..600, probe at 210. A ends at 100 (above the probe, gap at
    // the probe), B starts at 300: B's start is closer than A's (-400).
    let shift = 0;
    viewport.getBoundingClientRect = () => rect(0, 600);
    screen.getByTestId('message-a').getBoundingClientRect = () => rect(-400 + shift, 500);
    screen.getByTestId('message-b').getBoundingClientRect = () => rect(300 + shift, 900);
    viewport.scrollTop = 1000;
    const frames: FrameRequestCallback[] = [];
    vi.stubGlobal('requestAnimationFrame', (callback: FrameRequestCallback) => { frames.push(callback); return 1; });
    await fireEvent.scroll(viewport);
    frames.splice(0).forEach((frame) => frame(0));

    await fireEvent.click(await screen.findByTestId('timeline-jump-viewport-scroll-to-active-start'));
    expect(requests).toEqual([{ scrollTop: 1292, rowKey: 'row-b' }]);

    // No follow-up correction after later frames, even if layout moved
    // (it would fight iOS momentum scrolling).
    shift = -292 + 40;
    while (frames.length) frames.splice(0).forEach((frame) => frame(0));
    expect(requests).toHaveLength(1);
    vi.stubGlobal('requestAnimationFrame', (callback: FrameRequestCallback) => { callback(0); return 1; });
  });

  it.each([
    ['wheel', async (viewport: HTMLElement) => fireEvent.wheel(viewport, { deltaY: -120 })],
    ['touch', async (viewport: HTMLElement) => fireEvent.touchStart(viewport)],
    ['scrolling key', async (viewport: HTMLElement) => fireEvent.keyDown(viewport, { key: 'PageUp' })],
  ])('accepts %s scrolling before a previous programmatic frame settles', async (_label, armIntent) => {
    const previousFrame = globalThis.requestAnimationFrame;
    const frames: FrameRequestCallback[] = [];
    vi.stubGlobal('requestAnimationFrame', (callback: FrameRequestCallback) => {
      frames.push(callback);
      return frames.length;
    });
    try {
      render(TimelineViewportFixture);
      const viewport = screen.getByTestId('timeline-intent-viewport');
      Object.defineProperties(viewport, {
        scrollHeight: { configurable: true, value: 1000 },
        clientHeight: { configurable: true, value: 200 },
      });
      await fireEvent.click(screen.getByRole('button', { name: 'Programmatic bottom' }));
      expect(viewport.scrollTop).toBe(800);
      expect(frames.length).toBeGreaterThan(0);

      await armIntent(viewport);
      viewport.scrollTop = 400;
      await fireEvent.scroll(viewport);
      expect(screen.getByTestId('timeline-intent-state')).toHaveTextContent('paused');
      while (frames.length) frames.splice(0).forEach(frame => frame(0));
      expect(viewport.scrollTop).toBe(400);
      expect(screen.getByTestId('timeline-intent-state')).toHaveTextContent('paused');
    } finally {
      vi.stubGlobal('requestAnimationFrame', previousFrame);
    }
  });

  it('does not apply an already queued resize pin after the reader scrolls up', async () => {
    const previousFrame = globalThis.requestAnimationFrame;
    const previousObserver = globalThis.ResizeObserver;
    const frames: FrameRequestCallback[] = [];
    const resizes: Array<() => void> = [];
    vi.stubGlobal('requestAnimationFrame', (callback: FrameRequestCallback) => {
      frames.push(callback);
      return frames.length;
    });
    vi.stubGlobal('ResizeObserver', class {
      constructor(callback: () => void) { resizes.push(callback); }
      observe() {}
      disconnect() {}
    });
    try {
      render(TimelineViewportFixture, { autoScrollOnResize: true });
      const viewport = screen.getByTestId('timeline-intent-viewport');
      Object.defineProperties(viewport, {
        scrollHeight: { configurable: true, value: 1000 },
        clientHeight: { configurable: true, value: 200 },
      });
      viewport.scrollTop = 800;
      await fireEvent.scroll(viewport);
      frames.splice(0).forEach(frame => frame(0));
      resizes.forEach(resize => resize());
      frames.splice(0).forEach(frame => frame(0));
      expect(frames.length).toBeGreaterThan(0);

      await fireEvent.wheel(viewport, { deltaY: -120 });
      viewport.scrollTop = 400;
      await fireEvent.scroll(viewport);
      expect(screen.getByTestId('timeline-intent-state')).toHaveTextContent('paused');
      while (frames.length) frames.splice(0).forEach(frame => frame(0));
      expect(viewport.scrollTop).toBe(400);
    } finally {
      vi.stubGlobal('requestAnimationFrame', previousFrame);
      vi.stubGlobal('ResizeObserver', previousObserver);
    }
  });

  it('keeps exported programmatic scrolling out of manual-scroll state', async () => {
    render(TimelineViewportFixture);
    const viewport = screen.getByTestId('timeline-intent-viewport');
    Object.defineProperties(viewport, {
      scrollHeight: { configurable: true, value: 1000 },
      clientHeight: { configurable: true, value: 200 },
    });

    await fireEvent.wheel(viewport, { deltaY: -120 });
    await fireEvent.click(screen.getByRole('button', { name: 'Programmatic bottom' }));
    await fireEvent.scroll(viewport);

    expect(viewport.scrollTop).toBe(800);
    expect(screen.getByTestId('timeline-intent-state')).toHaveTextContent('following');
  });
});
