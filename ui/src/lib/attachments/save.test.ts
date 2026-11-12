import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { downloadBlob, downloadUrl } from './save';

describe('shared file downloads', () => {
  beforeEach(() => {
    vi.useFakeTimers();
    URL.createObjectURL = vi.fn(() => 'blob:https://cognis.test/8d6feafb-2aac-4b41-ac82-42b2314563bb');
    URL.revokeObjectURL = vi.fn();
  });
  afterEach(() => { vi.useRealTimers(); vi.restoreAllMocks(); });

  it('keeps binary bytes and the named attached anchor alive through asynchronous WebKit consumption', () => {
    const bytes = new Blob([new Uint8Array([0, 1, 127, 128, 255])]);
    const click = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(function (this: HTMLAnchorElement) {
      expect(this.isConnected).toBe(true);
      expect(this.download).toBe('výsledek.zip');
      expect(this.target).toBe('');
      expect(this.href).toBe('blob:https://cognis.test/8d6feafb-2aac-4b41-ac82-42b2314563bb');
    });
    downloadBlob(bytes, 'výsledek.zip');
    expect(URL.createObjectURL).toHaveBeenCalledWith(bytes);
    expect(click).toHaveBeenCalledOnce();
    expect(document.querySelector('a[download]')).toBeNull();
    expect(URL.revokeObjectURL).not.toHaveBeenCalled();
    vi.advanceTimersByTime(60_000);
    expect(URL.revokeObjectURL).toHaveBeenCalledOnce();
  });

  it('marks signed URL saves explicitly without opening a browser window', () => {
    const open = vi.spyOn(window, 'open');
    vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(function (this: HTMLAnchorElement) {
      expect(this.download).toBe('guide.md');
      expect(this.href).toContain('/signed/guide.md?signature=abc');
      expect(this.target).toBe('');
      expect(this.referrerPolicy).toBe('no-referrer');
    });
    downloadUrl('/signed/guide.md?signature=abc', 'guide.md');
    expect(open).not.toHaveBeenCalled();
  });

  it('cleans up both the anchor and object URL even when clicking fails', () => {
    vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => { throw new Error('Click failed'); });
    expect(() => downloadBlob(new Blob(['text']), 'workflow.yaml')).toThrow('Click failed');
    expect(document.querySelector('a[download]')).toBeNull();
    vi.advanceTimersByTime(60_000);
    expect(URL.revokeObjectURL).toHaveBeenCalledOnce();
  });
});
