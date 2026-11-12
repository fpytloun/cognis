import { afterEach, describe, expect, it, vi } from 'vitest';
import { CAPTURE_MAX_BYTES, captureDimensions, captureScreen, checkedCrop, cropCapture, rasterDimensions } from './capture';

function png(width: number, height: number) {
  const buffer = new ArrayBuffer(32);
  const bytes = new Uint8Array(buffer);
  bytes.set([137, 80, 78, 71, 13, 10, 26, 10]);
  const view = new DataView(buffer);
  view.setUint32(8, 13); view.setUint32(12, 0x49484452);
  view.setUint32(16, width); view.setUint32(20, height);
  return new Blob([buffer], { type: 'image/png' });
}

afterEach(() => { vi.unstubAllGlobals(); vi.restoreAllMocks(); });

describe('bounded transient observed capture', () => {
  it('checks raster signatures, dimensions, pixel and byte budgets before decoding', async () => {
    await expect(rasterDimensions(png(800, 600))).resolves.toEqual({ width: 800, height: 600 });
    await expect(rasterDimensions(png(10000, 10000))).rejects.toThrow('16 megapixels');
    await expect(rasterDimensions(new Blob(['<svg onload="fetch()"/>'], { type: 'image/png' }))).rejects.toThrow('raster');
    await expect(rasterDimensions(new Blob([new Uint8Array(CAPTURE_MAX_BYTES + 1)], { type: 'image/png' }))).rejects.toThrow('8 MiB');
    expect(() => captureDimensions(Infinity, 1)).toThrow();
    expect(() => captureDimensions(0, 1)).toThrow();
  });

  it('rejects out-of-bounds and fractional crops', () => {
    const bounds = { width: 100, height: 100 };
    expect(checkedCrop(bounds, { x: 10, y: 10, width: 90, height: 90 })).toEqual({ x: 10, y: 10, width: 90, height: 90 });
    for (const crop of [
      { x: -1, y: 0, width: 1, height: 1 },
      { x: 99, y: 0, width: 2, height: 1 },
      { x: 0, y: 0, width: 0, height: 1 },
      { x: 0.5, y: 0, width: 1, height: 1 },
    ]) expect(() => checkedCrop(bounds, crop)).toThrow('Crop');
  });

  it('stops a capture granted after cancellation and never decodes pixels', async () => {
    const stop = vi.fn();
    const stream = { getTracks: () => [{ stop }] };
    const controller = new AbortController();
    const getDisplayMedia = vi.fn(async () => { controller.abort(); return stream; });
    vi.stubGlobal('navigator', { mediaDevices: { getDisplayMedia } });
    vi.spyOn(HTMLMediaElement.prototype, 'pause').mockImplementation(() => {});
    await expect(captureScreen(controller.signal)).rejects.toThrow();
    expect(stop).toHaveBeenCalledTimes(1);
    expect(getDisplayMedia).toHaveBeenCalledWith({ video: true, audio: false });
  });

  it('offers honest fallback when screen capture is unsupported', async () => {
    vi.stubGlobal('navigator', {});
    await expect(captureScreen(new AbortController().signal)).rejects.toThrow('Choose or paste');
  });

  it('decodes and reencodes only the confirmed crop, releasing pixel resources', async () => {
    const close = vi.fn();
    const drawImage = vi.fn();
    const bitmap = { width: 800, height: 600, close };
    vi.stubGlobal('createImageBitmap', vi.fn(async () => bitmap));
    vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue({ drawImage } as unknown as CanvasRenderingContext2D);
    vi.spyOn(HTMLCanvasElement.prototype, 'toBlob').mockImplementation(callback => callback(png(100, 200)));
    const result = await cropCapture(png(800, 600), { x: 20, y: 30, width: 100, height: 200 }, new AbortController().signal);
    expect(drawImage).toHaveBeenCalledWith(bitmap, 20, 30, 100, 200, 0, 0, 100, 200);
    expect(result.type).toBe('image/png');
    expect(close).toHaveBeenCalledTimes(1);
  });
});
