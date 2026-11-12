import { expect, it, vi } from 'vitest';
import { observeSourceWorkspace, sourceBounds } from './source-bounds';

it('uses a stable scroll viewport rather than the self-constrained source height', () => {
  const viewport = document.createElement('div'), source = document.createElement('div');
  viewport.style.overflowY = 'auto'; viewport.append(source); document.body.append(viewport);
  Object.defineProperties(viewport, { clientHeight: { value: 600 }, clientWidth: { value: 340 } });
  vi.spyOn(viewport, 'getBoundingClientRect').mockReturnValue(new DOMRect(200, 40, 340, 600));
  const box = vi.spyOn(source, 'getBoundingClientRect').mockReturnValue(new DOMRect(204, 100, 332, 400));
  expect(sourceBounds(source)).toEqual({ left: 204, top: 40, width: 332, height: 600 });
  box.mockReturnValue(new DOMRect(204, 100, 332, 24));
  expect(sourceBounds(source)).toEqual({ left: 204, top: 40, width: 332, height: 600 });
  viewport.remove();
  expect(sourceBounds(source)).toBeNull();
});

it('owns accommodation and detaches all queued measurement inputs on disposal', () => {
  const source = document.createElement('div'); document.body.append(source);
  vi.spyOn(source, 'getBoundingClientRect').mockReturnValue(new DOMRect(204, 100, 332, 400));
  const report = vi.fn(), height = vi.fn();
  const stop = observeSourceWorkspace(source, report, { visibleBottom: 220, viewportHeight: 844 }, height);
  expect(height).toHaveBeenLastCalledWith(120);
  report.mockClear(); height.mockClear();
  window.dispatchEvent(new Event('resize'));
  expect(report).toHaveBeenCalledTimes(1); expect(height).toHaveBeenCalledTimes(1);
  stop(); window.dispatchEvent(new Event('resize')); window.dispatchEvent(new Event('scroll'));
  expect(report).toHaveBeenCalledTimes(1); expect(height).toHaveBeenCalledTimes(1);
  source.remove();
});
