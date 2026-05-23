import { describe, expect, it, vi } from 'vitest';

import { pastedFileFingerprint, pastedFilesFromClipboardData, readPastedFilesFromNavigator } from '$lib/clipboard';

function clipboardDataWithFiles(files: File[]): DataTransfer {
  return { files, items: [] } as unknown as DataTransfer;
}

function clipboardDataWithItems(files: File[]): DataTransfer {
  return {
    files: [],
    items: files.map((file) => ({ kind: 'file', getAsFile: () => file }))
  } as unknown as DataTransfer;
}

describe('clipboard file helpers', () => {
  it('extracts pasted PDF files from clipboardData.files', () => {
    const file = new File(['pdf'], 'report.pdf', { type: 'application/pdf' });

    const result = pastedFilesFromClipboardData(clipboardDataWithFiles([file]));

    expect(result).toEqual([file]);
  });

  it('extracts pasted PDF files from clipboardData.items', () => {
    const file = new File(['pdf'], 'report.pdf', { type: 'application/pdf' });

    const result = pastedFilesFromClipboardData(clipboardDataWithItems([file]));

    expect(result).toEqual([file]);
  });

  it('deduplicates pasted screenshots with unstable filenames', () => {
    const first = new File(['image'], 'image.png', { type: 'image/png', lastModified: 100 });
    const duplicate = new File(['image'], 'Screenshot 2026-05-20 at 12.00.00.png', {
      type: 'image/png',
      lastModified: 200
    });

    const result = pastedFilesFromClipboardData(clipboardDataWithFiles([first, duplicate]));

    expect(result).toEqual([first]);
    expect(pastedFileFingerprint(first)).toBe(pastedFileFingerprint(duplicate));
  });

  it('assigns a PDF filename to async clipboard blobs', async () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date('2026-05-06T12:00:00Z'));
    const blob = new Blob(['pdf'], { type: 'application/pdf' });
    const navigatorLike = {
      clipboard: {
        read: async () => [
          {
            types: ['application/pdf'],
            getType: async () => blob
          }
        ]
      }
    } as unknown as Pick<Navigator, 'clipboard'>;

    const result = await readPastedFilesFromNavigator(navigatorLike);

    expect(result).toHaveLength(1);
    expect(result[0].name).toBe('pasted-document-1778068800000.pdf');
    expect(result[0].type).toBe('application/pdf');
    vi.useRealTimers();
  });
});
