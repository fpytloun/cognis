import { fireEvent, render, screen } from '@testing-library/svelte';
import { beforeAll, describe, expect, it, vi } from 'vitest';

import TimelineViewportFixture from './TimelineViewport.test-fixture.svelte';

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
