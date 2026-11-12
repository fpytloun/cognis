import { describe, expect, it, vi } from 'vitest';
import { nativeSelection } from './native-selection';
import type { FeedbackSurfaceBinding } from '../types';

function setup() {
  const root = document.createElement('div'); root.textContent = 'Repeated repeated text'; document.body.append(root);
  const binding = { mode: 'comment', tool: 'text', context: { kind: 'standalone' }, source: { source_token: 'native-source', source: { kind: 'document' }, capabilities: { read: true, comment: true }, availability: 'available' },
    onTargetChange: vi.fn(), onInteractionChange: vi.fn() } as unknown as FeedbackSurfaceBinding;
  const map = vi.fn(async () => ({ candidate: { locator: { kind: 'whole' as const } }, label: 'Selected text' }));
  const options = { binding, instance: 'native-render-instance', map, error: vi.fn() };
  const action = nativeSelection(root, options);
  return { root, binding, map, options, action };
}
async function selection(root: HTMLElement, from: number, to: number) {
  const native = document.getSelection()!;
  native.removeAllRanges(); native.setBaseAndExtent(root.firstChild!, from, root.firstChild!, to);
  document.dispatchEvent(new Event('selectionchange'));
  await Promise.resolve(); await Promise.resolve(); await Promise.resolve();
}
describe('shared native selection lifecycle', () => {
  it('offers text comments without comment mode, but not while drawing or marking areas', async () => {
    const { root, binding, options, action, map } = setup();
    action.update({ ...options, binding: { ...binding, mode: 'draw' } });
    await selection(root, 0, 8);
    expect(map).not.toHaveBeenCalled();
    action.update({ ...options, binding: { ...binding, mode: 'view' } });
    await selection(root, 9, 17);
    expect(map).toHaveBeenCalledOnce();
    expect(binding.onTargetChange).toHaveBeenCalledWith(expect.objectContaining({ label: 'Selected text' }));
    action.destroy(); root.remove();
  });
  it('waits for a mouse drag to finish before publishing, so comment UI never mounts under the pointer', async () => {
    const { root, map, binding, action } = setup();
    document.dispatchEvent(new PointerEvent('pointerdown', { pointerType: 'mouse', button: 0, bubbles: true }));
    await selection(root, 0, 8);
    await selection(root, 0, 17);
    expect(map).not.toHaveBeenCalled();
    document.dispatchEvent(new PointerEvent('pointerup', { pointerType: 'mouse', button: 0, bubbles: true }));
    await Promise.resolve(); await Promise.resolve(); await Promise.resolve();
    expect(map).toHaveBeenCalledOnce();
    expect(binding.onTargetChange).toHaveBeenLastCalledWith(expect.objectContaining({ label: 'Selected text' }));
    action.destroy(); root.remove();
  });
  it('ignores clicks/Enter and observes backward and touch-handle selection changes without accepting', async () => {
    const { root, map, binding, action } = setup();
    root.dispatchEvent(new MouseEvent('click')); root.dispatchEvent(new KeyboardEvent('keyup', { key: 'Enter' }));
    await Promise.resolve(); expect(map).not.toHaveBeenCalled();
    await selection(root, 17, 9); expect(map).toHaveBeenCalledOnce();
    expect(binding.onTargetChange).toHaveBeenLastCalledWith(expect.objectContaining({ label: 'Selected text' }));
    await selection(root, 21, 9); expect(map).toHaveBeenCalledTimes(2);
    action.destroy(); root.remove();
  });
  it('rejects stale asynchronous mapping after tool or source replacement', async () => {
    const { root, binding, options, action } = setup();
    let finish!: (value: Awaited<ReturnType<typeof options.map>>) => void;
    options.map.mockImplementationOnce(() => new Promise(resolve => { finish = resolve; }));
    await selection(root, 0, 8);
    action.update({ ...options, binding: { ...binding, source: { ...binding.source, source_token: 'replacement-source' } } });
    finish({ candidate: { locator: { kind: 'whole' } }, label: 'Late text' });
    await Promise.resolve(); await Promise.resolve();
    expect(binding.onTargetChange).not.toHaveBeenCalledWith(expect.objectContaining({ label: 'Late text' }));
    action.destroy(); root.remove();
  });
});
