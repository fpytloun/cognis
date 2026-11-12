// WK HTTPS documents cannot fetch custom-scheme binary resources either.
// Keep this module native-only; the web build retains separately served assets.
const resources = import.meta.glob<string>(
  '/node_modules/pdfjs-dist/{cmaps,standard_fonts}/*.{bcmap,pfb,ttf}',
  { query: '?url&inline', import: 'default', eager: true },
);
const directories: Record<string, string> = {
  cMapUrl: 'cmaps', standardFontDataUrl: 'standard_fonts',
};

export class BundledPdfBinaryData {
  async fetch({ kind, filename }: { kind: string; filename: string }): Promise<Uint8Array<ArrayBuffer>> {
    const directory = directories[kind];
    const value = directory && resources[`/node_modules/pdfjs-dist/${directory}/${filename}`];
    if (!value?.startsWith('data:') || !value.includes(';base64,')) {
      throw new Error('Missing bundled PDF binary resource');
    }
    const bytes = atob(value.slice(value.indexOf(',') + 1));
    return Uint8Array.from(bytes, char => char.charCodeAt(0));
  }
}
