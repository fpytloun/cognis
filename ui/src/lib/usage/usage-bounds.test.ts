import { afterEach, describe, expect, it, vi } from 'vitest';
import { usageBounds } from '../../../e2e/usage-bounds';

const originalElementFromPoint = Object.getOwnPropertyDescriptor(document, 'elementFromPoint');
afterEach(() => {
  document.body.replaceChildren();
  vi.unstubAllGlobals();
  if (originalElementFromPoint) Object.defineProperty(document, 'elementFromPoint', originalElementFromPoint);
  else Reflect.deleteProperty(document, 'elementFromPoint');
});

function overflowing(tag: string, options: { marked?: boolean; clipped?: boolean; size?: number } = {}) {
  const root = document.createElement('main');
  const node = document.createElement(tag);
  node.textContent = 'Usage scope';
  if (options.marked) node.className = 'sr-only';
  if (options.clipped) {
    node.style.cssText = 'position:absolute;overflow:hidden;clip:rect(0px, 0px, 0px, 0px)';
  }
  const size = options.size ?? 1;
  node.getBoundingClientRect = () => new DOMRect(0, 0, size, size);
  node.checkVisibility = () => true;
  Object.defineProperties(node, { clientWidth: { value: size }, scrollWidth: { value: 90 } });
  root.append(node);
  document.body.append(root);
  return { root, node };
}

describe('painted-text overflow', () => {
  it('keeps the semantic legend but excludes its explicitly hidden 1x1 clipped text', () => {
    const { root, node } = overflowing('legend', { marked: true, clipped: true });
    expect(usageBounds(root).overflow).toEqual([]);
    expect(root.querySelector('legend')).toBe(node);
    expect(node.textContent).toBe('Usage scope');
  });

  it.each([
    { marked: false, clipped: true },
    { marked: true, clipped: false },
    { marked: true, clipped: true, size: 20 },
  ])('still catches a clipped visible legend or an unproven sr-only label: %j', (options: { marked: boolean; clipped: boolean; size?: number }) => {
    const { root } = overflowing('legend', options);
    expect(usageBounds(root).overflow).toEqual(['LEGEND:Usage scope']);
  });

  it('does not exempt visible text merely because a parent has an sr-only marker', () => {
    const { root } = overflowing('p', { clipped: true });
    root.className = 'sr-only';
    expect(usageBounds(root).overflow).toEqual(['P:Usage scope']);
  });

  it('continues measuring actual controls, their size, and hit results', () => {
    const root = document.createElement('main');
    const button = document.createElement('button');
    button.textContent = 'Refresh';
    button.checkVisibility = () => true;
    button.getBoundingClientRect = () => new DOMRect(10, 10, 35, 35);
    root.append(button);
    document.body.append(root);
    vi.stubGlobal('innerWidth', 390);
    vi.stubGlobal('innerHeight', 844);
    Object.defineProperty(document, 'elementFromPoint', { configurable: true, value: () => button });
    expect(usageBounds(root).targets).toEqual([
      { name: 'Refresh', x: 10, y: 10, width: 35, height: 35, tag: 'BUTTON', hit: true }
    ]);
  });
});

describe('painted target centers', () => {
  function scrollport(overflow = 'auto') {
    const root = document.createElement('main');
    root.style.overflow = overflow;
    root.getBoundingClientRect = () => new DOMRect(0, 116, 390, 674);
    Object.defineProperties(root, {
      clientLeft: { value: 0 }, clientTop: { value: 1 },
      clientWidth: { value: 390 }, clientHeight: { value: 672 },
      offsetWidth: { value: 390 }, offsetHeight: { value: 674 },
    });
    document.body.append(root);
    vi.stubGlobal('innerWidth', 390);
    vi.stubGlobal('innerHeight', 844);
    return root;
  }

  function target(root: Element, y: number, height: number, tag = 'button') {
    const node = document.createElement(tag);
    node.textContent = 'Inspect filtered requests';
    node.checkVisibility = () => true;
    node.getBoundingClientRect = () => new DOMRect(24.5, y, 341, height);
    root.append(node);
    return node;
  }

  it.each(['auto', 'scroll', 'hidden', 'clip'])(
    'keeps full size measurements but does not hit-test centers outside an %s ancestor clip',
    (overflow) => {
      const root = scrollport(overflow);
      target(root, 31.421875, 63.5);
      target(root, 807.078125, 44, 'summary');
      const hit = vi.fn(() => document.createElement('header'));
      Object.defineProperty(document, 'elementFromPoint', { configurable: true, value: hit });
      const measured = usageBounds(root).targets;
      expect(measured.map(({ height, hit }) => ({ height, hit }))).toEqual([
        { height: 63.5, hit: null }, { height: 44, hit: null },
      ]);
      expect(hit).not.toHaveBeenCalled();
    },
  );

  it.each(['button', 'summary'])('still rejects actual visible overlap on a %s', (tag) => {
    const root = scrollport();
    // Its top is clipped, but its center is inside the real client box.
    target(root, 100, 44, tag);
    const blocker = document.createElement('header');
    Object.defineProperty(document, 'elementFromPoint', { configurable: true, value: () => blocker });
    expect(usageBounds(root).targets[0].hit).toBe(false);
  });

  it.each(['sticky', 'static'])('distinguishes the known %s navigation from an accidental overlay', (position) => {
    const root = scrollport();
    const navigation = document.createElement('div');
    navigation.dataset.usageStickyNavigation = '';
    navigation.style.position = position;
    root.append(navigation);
    const control = target(navigation, 120, 44);
    target(root, 120, 44);
    Object.defineProperty(document, 'elementFromPoint', { configurable: true, value: () => control });
    expect(usageBounds(root).targets.map(({ hit }) => hit)).toEqual([
      true, position === 'sticky' ? null : false,
    ]);
  });

  it('checks every clipping ancestor and respects borders rather than the outer box', () => {
    const root = scrollport();
    const nested = document.createElement('div');
    nested.style.overflowY = 'hidden';
    nested.getBoundingClientRect = () => new DOMRect(0, 200, 390, 200);
    Object.defineProperties(nested, {
      clientTop: { value: 10 }, clientHeight: { value: 180 },
      offsetHeight: { value: 200 },
    });
    root.append(nested);
    target(nested, 180, 44); // Center 202 is inside the border, outside the client clip.
    const painted = target(nested, 210, 44);
    Object.defineProperty(document, 'elementFromPoint', { configurable: true, value: () => painted });
    expect(usageBounds(root).targets.map(({ hit }) => hit)).toEqual([null, true]);
  });

  it('does not treat an ordinary ancestor box as a clip or exempt header overlap', () => {
    const root = scrollport('visible');
    target(root, 31.421875, 63.5);
    Object.defineProperty(document, 'elementFromPoint', {
      configurable: true, value: () => document.createElement('header'),
    });
    expect(usageBounds(root).targets[0].hit).toBe(false);
  });
});
