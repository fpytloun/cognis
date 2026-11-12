export const CAPTURE_MAX_BYTES = 8 * 1024 * 1024;
export const CAPTURE_MAX_PIXELS = 16_000_000;
const MAX_DIMENSION = 16_384;

export function captureDimensions(width: number, height: number): void {
  if (!Number.isSafeInteger(width) || !Number.isSafeInteger(height)
    || width <= 0 || height <= 0 || width > MAX_DIMENSION || height > MAX_DIMENSION
    || width * height > CAPTURE_MAX_PIXELS) {
    throw new Error('Use a screenshot of at most 16 megapixels and 16,384 pixels per side.');
  }
}

/** Validate the raster header before asking a browser decoder to allocate pixels. */
export async function rasterDimensions(blob: Blob) {
  if (blob.size > CAPTURE_MAX_BYTES || blob.size < 12) throw new Error('Use a PNG or WebP screenshot of at most 8 MiB.');
  const bytes = new Uint8Array(await blob.slice(0, 32).arrayBuffer());
  const view = new DataView(bytes.buffer);
  let width = 0;
  let height = 0;
  if (blob.type === 'image/png' && bytes.length >= 24
    && [137, 80, 78, 71, 13, 10, 26, 10].every((value, index) => bytes[index] === value)
    && view.getUint32(8) === 13 && view.getUint32(12) === 0x49484452) {
    width = view.getUint32(16); height = view.getUint32(20);
  } else if (blob.type === 'image/webp' && bytes.length >= 30
    && view.getUint32(0) === 0x52494646 && view.getUint32(8) === 0x57454250
    && view.getUint32(4, true) + 8 === blob.size) {
    const kind = view.getUint32(12);
    if (kind === 0x56503858 && view.getUint32(16, true) === 10) { // VP8X
      if (bytes[20] & 2) throw new Error('Animated screenshots are not supported.');
      width = 1 + bytes[24] + (bytes[25] << 8) + (bytes[26] << 16);
      height = 1 + bytes[27] + (bytes[28] << 8) + (bytes[29] << 16);
    } else if (kind === 0x56503820 && bytes[23] === 0x9d && bytes[24] === 0x01 && bytes[25] === 0x2a) { // VP8
      width = view.getUint16(26, true) & 0x3fff;
      height = view.getUint16(28, true) & 0x3fff;
    } else if (kind === 0x5650384c && bytes[20] === 0x2f) { // VP8L
      width = 1 + bytes[21] + ((bytes[22] & 0x3f) << 8);
      height = 1 + (bytes[22] >> 6) + (bytes[23] << 2) + ((bytes[24] & 0x0f) << 10);
    }
  }
  if (!width || !height) throw new Error('The screenshot is not a supported PNG or WebP raster.');
  captureDimensions(width, height);
  return { width, height };
}

function canvasBlob(canvas: HTMLCanvasElement): Promise<Blob> {
  return new Promise((resolve, reject) => canvas.toBlob(blob => {
    if (!blob || blob.size > CAPTURE_MAX_BYTES) reject(new Error('The captured image exceeds the 8 MiB limit. Choose a smaller crop.'));
    else resolve(blob);
  }, 'image/png'));
}

/** Local-only candidate. Nothing is uploaded or adopted until trusted chrome confirms a crop. */
export async function captureScreen(signal: AbortSignal): Promise<Blob> {
  if (!navigator.mediaDevices?.getDisplayMedia) throw new Error('Screen capture is unavailable. Choose or paste a PNG or WebP screenshot.');
  signal.throwIfAborted();
  const stream = await navigator.mediaDevices.getDisplayMedia({ video: true, audio: false });
  const stop = () => stream.getTracks().forEach(track => track.stop());
  const video = document.createElement('video');
  video.muted = true;
  video.playsInline = true;
  video.srcObject = stream;
  signal.addEventListener('abort', stop, { once: true });
  try {
    signal.throwIfAborted();
    await new Promise<void>((resolve, reject) => {
      const cleanup = () => {
        clearTimeout(timer);
        signal.removeEventListener('abort', aborted);
        video.removeEventListener('loadeddata', loaded);
        video.removeEventListener('error', failed);
      };
      const loaded = () => { cleanup(); resolve(); };
      const failed = () => { cleanup(); reject(new Error('The selected screen could not be captured. Choose a screenshot instead.')); };
      const aborted = () => { cleanup(); reject(signal.reason ?? new DOMException('Capture cancelled', 'AbortError')); };
      const timer = setTimeout(failed, 15_000);
      video.addEventListener('loadeddata', loaded, { once: true });
      video.addEventListener('error', failed, { once: true });
      signal.addEventListener('abort', aborted, { once: true });
      void video.play().catch(failed);
    });
    signal.throwIfAborted();
    captureDimensions(video.videoWidth, video.videoHeight);
    const canvas = document.createElement('canvas');
    try {
      canvas.width = video.videoWidth;
      canvas.height = video.videoHeight;
      const context = canvas.getContext('2d');
      if (!context) throw new Error('This browser cannot capture pixels. Choose a screenshot instead.');
      context.drawImage(video, 0, 0);
      const blob = await canvasBlob(canvas);
      signal.throwIfAborted();
      return blob;
    } finally { canvas.width = 0; canvas.height = 0; }
  } finally {
    signal.removeEventListener('abort', stop);
    stop();
    video.pause();
    video.srcObject = null;
  }
}

export function checkedCrop(
  bounds: { width: number; height: number },
  crop: { x: number; y: number; width: number; height: number },
) {
  captureDimensions(bounds.width, bounds.height);
  if (![crop.x, crop.y, crop.width, crop.height].every(Number.isSafeInteger)
    || crop.x < 0 || crop.y < 0 || crop.width <= 0 || crop.height <= 0
    || crop.x + crop.width > bounds.width || crop.y + crop.height > bounds.height) {
    throw new Error('Crop entirely within the screenshot using whole pixels.');
  }
  return { ...crop };
}

/** Freeze only confirmed pixels; do not retain or upload the uncropped screen. */
export async function cropCapture(
  blob: Blob, crop: { x: number; y: number; width: number; height: number }, signal: AbortSignal,
): Promise<Blob> {
  signal.throwIfAborted();
  checkedCrop(await rasterDimensions(blob), crop);
  const bitmap = await createImageBitmap(blob);
  const canvas = document.createElement('canvas');
  try {
    signal.throwIfAborted();
    checkedCrop({ width: bitmap.width, height: bitmap.height }, crop);
    canvas.width = crop.width; canvas.height = crop.height;
    const context = canvas.getContext('2d');
    if (!context) throw new Error('This browser cannot crop screenshots.');
    context.drawImage(bitmap, crop.x, crop.y, crop.width, crop.height, 0, 0, crop.width, crop.height);
    const frozen = await canvasBlob(canvas);
    signal.throwIfAborted();
    return frozen;
  } finally { bitmap.close(); canvas.width = 0; canvas.height = 0; }
}
