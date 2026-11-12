import { afterEach, beforeEach, describe, expect, it } from 'vitest';

import { keepInViewport } from './keep-in-viewport';

function stubRect(node: HTMLElement, rect: () => { left: number; width: number }) {
  node.getBoundingClientRect = () => {
    const { left, width } = rect();
    return { left, right: left + width, width, top: 0, bottom: 10, height: 10, x: left, y: 0, toJSON: () => ({}) } as DOMRect;
  };
}

describe('keepInViewport', () => {
  beforeEach(() => {
    Object.defineProperty(document.documentElement, 'clientWidth', { configurable: true, value: 390 });
  });
  afterEach(() => {
    document.body.innerHTML = '';
  });

  it('shifts a popup that overflows the left edge back inside', () => {
    const node = document.createElement('div');
    document.body.appendChild(node);
    stubRect(node, () => ({ left: -120, width: 288 }));
    const action = keepInViewport(node);
    expect(node.style.translate).toBe('128px 0');
    action.destroy();
  });

  it('shifts a popup that overflows the right edge back inside', () => {
    const node = document.createElement('div');
    document.body.appendChild(node);
    stubRect(node, () => ({ left: 200, width: 288 }));
    const action = keepInViewport(node);
    expect(node.style.translate).toBe('-106px 0');
    action.destroy();
  });

  it('leaves a popup that fits untouched and caps one wider than the viewport', () => {
    const fits = document.createElement('div');
    document.body.appendChild(fits);
    stubRect(fits, () => ({ left: 20, width: 200 }));
    keepInViewport(fits).destroy();
    expect(fits.style.translate).toBe('');

    const wide = document.createElement('div');
    document.body.appendChild(wide);
    let width = 600;
    stubRect(wide, () => ({ left: 0, width }));
    const action = keepInViewport(wide);
    expect(wide.style.maxWidth).toBe('374px');
    // Simulate the browser applying the cap on the next measurement.
    width = 374;
    action.update();
    expect(wide.style.translate).toBe('8px 0');
    action.destroy();
  });

  it('flips a bottom-edge menu above its trigger', () => {
    const details = document.createElement('details');
    const summary = document.createElement('summary');
    const node = document.createElement('div');
    details.append(summary, node);
    document.body.appendChild(details);
    Object.defineProperty(window, 'innerHeight', { configurable: true, value: 844 });
    summary.getBoundingClientRect = () => ({ top: 780, bottom: 824 } as DOMRect);
    node.getBoundingClientRect = () => ({ left: 200, right: 380, width: 180, top: 830, bottom: 1030, height: 200 } as DOMRect);
    const action = keepInViewport(node, { vertical: true });
    expect(node.style.translate).toBe('0px -256px');
    expect(node.style.maxHeight).toBe('766px');
    action.destroy();
  });

  it('constrains a tall menu to the space above its trigger without detaching it', () => {
    const details = document.createElement('details');
    const summary = document.createElement('summary');
    const node = document.createElement('div');
    details.append(summary, node);
    document.body.appendChild(details);
    Object.defineProperty(window, 'innerHeight', { configurable: true, value: 844 });
    summary.getBoundingClientRect = () => ({ top: 500, bottom: 544 } as DOMRect);
    node.getBoundingClientRect = () => {
      const height = Math.min(1000, parseFloat(node.style.maxHeight) || 1000);
      return { left: 200, right: 380, width: 180, top: 494 - height, bottom: 494, height } as DOMRect;
    };
    const action = keepInViewport(node, { vertical: true, preferredSide: 'above' });
    expect(node.style.maxHeight).toBe('486px');
    expect(node.style.translate).toBe('');
    expect(node.style.overflowY).toBe('auto');
    action.update({ vertical: true, preferredSide: 'above' });
    expect(node.style.translate).toBe('');
    action.destroy();
  });
});
