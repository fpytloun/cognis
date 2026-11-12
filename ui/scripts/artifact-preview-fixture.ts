// Synthetic inputs through the production renderer, under the real native CSP.
import { mount, unmount, type ComponentProps } from 'svelte';
import ArtifactPreviewContent from '../src/lib/components/ArtifactPreviewContent.svelte';
import { BundledPdfBinaryData } from '../src/lib/native/pdf-binary-data';
import '../src/app.css';

let mounted: ReturnType<typeof mount> | undefined;
let mediaUrl: string | undefined;
const target = document.getElementById('fixture')!;
target.style.cssText = 'height:600px;width:800px';
const diagnostics: string[] = [];
for (const level of ['warn', 'error'] as const) {
  const original = console[level].bind(console);
  console[level] = (...values: unknown[]) => { diagnostics.push(values.map(String).join(' ')); original(...values); };
}
document.addEventListener('securitypolicyviolation', event => {
  diagnostics.push(`CSP ${event.violatedDirective} ${event.blockedURI}`);
});
const NativeWorker = globalThis.Worker;
let activeWorkers = 0;
globalThis.Worker = class extends NativeWorker {
  private stopped = false;
  constructor(url: string | URL, options?: WorkerOptions) {
    super(url, options);
    activeWorkers++;
    this.addEventListener('error', event => diagnostics.push(`Worker: ${event.message}`));
  }
  terminate() {
    if (!this.stopped) { activeWorkers--; this.stopped = true; }
    super.terminate();
  }
};

function pdf(image = false, a4 = false): Uint8Array<ArrayBuffer> {
  const objects = [
    '<< /Type /Catalog /Pages 2 0 R >>',
    '<< /Type /Pages /Kids [3 0 R 4 0 R] /Count 2 >>',
    '<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 300] /Resources << /Font << /F1 5 0 R >> >> /Contents 6 0 R >>',
    '<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 300] /Resources << /Font << /F1 5 0 R >> >> /Contents 6 0 R >>',
    '<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>',
  ];
  if (a4) {
    objects[2] = objects[2].replace('300 300', '595 842');
    objects[3] = objects[3].replace('300 300', '595 842');
  }
  const stream = '1 0 0 rg 20 20 100 100 re f BT /F1 24 Tf 20 150 Td (Native PDF) Tj ET\n';
  objects.push(`<< /Length ${stream.length} >>\nstream\n${stream}endstream`);
  if (image) {
    const bytes = atob('__JPEG2000_BASE64__');
    objects[2] = objects[2].replace('/Font <<', '/XObject << /Im1 7 0 R >> /Font <<');
    const imageStream = 'q 100 0 0 100 20 20 cm /Im1 Do Q\n';
    objects[5] = `<< /Length ${imageStream.length} >>\nstream\n${imageStream}endstream`;
    objects.push(`<< /Type /XObject /Subtype /Image /Width 16 /Height 16 /ColorSpace /DeviceRGB /BitsPerComponent 8 /Filter /JPXDecode /Length ${bytes.length} >>\nstream\n${bytes}\nendstream`);
  }
  let result = '%PDF-1.7\n';
  const offsets = [0];
  objects.forEach((value, index) => {
    offsets.push(result.length);
    result += `${index + 1} 0 obj\n${value}\nendobj\n`;
  });
  const xref = result.length;
  result += `xref\n0 ${offsets.length}\n0000000000 65535 f \n`;
  result += offsets.slice(1).map(offset => `${String(offset).padStart(10, '0')} 00000 n \n`).join('');
  result += `trailer\n<< /Size ${offsets.length} /Root 1 0 R >>\nstartxref\n${xref}\n%%EOF\n`;
  return Uint8Array.from(result, char => char.charCodeAt(0));
}

const close = async () => {
  if (mounted) await unmount(mounted);
  mounted = undefined;
  if (mediaUrl) URL.revokeObjectURL(mediaUrl);
  mediaUrl = undefined;
};
Object.assign(window, { artifactPreviewFixture: {
  diagnostics,
  get activeWorkers() { return activeWorkers; },
  async validateResources() {
    const factory = new BundledPdfBinaryData();
    const cmap = await factory.fetch({ kind: 'cMapUrl', filename: 'Adobe-Japan1-UCS2.bcmap' });
    const font = await factory.fetch({ kind: 'standardFontDataUrl', filename: 'LiberationSans-Regular.ttf' });
    const url = URL.createObjectURL(new Blob([font]));
    try {
      const face = new FontFace('FixturePdfFont', `url("${url}")`);
      await face.load();
      return cmap.length > 0 && face.status === 'loaded';
    } finally { URL.revokeObjectURL(url); }
  },
  async open(kind: string) {
    await close();
    const props: ComponentProps<typeof ArtifactPreviewContent> = {
      kind: kind as 'pdf', filename: `fixture.${kind}`,
    };
    if (kind === 'pdf' || kind === 'jpx-pdf' || kind === 'a4-pdf' || kind === 'invalid-pdf') {
      props.kind = 'pdf';
      mediaUrl = URL.createObjectURL(new Blob([kind === 'invalid-pdf' ? 'not a PDF' : pdf(kind === 'jpx-pdf', kind === 'a4-pdf')], { type: 'application/pdf' }));
      props.mediaUrl = mediaUrl;
    } else if (kind === 'image') {
      props.mediaUrl = 'data:image/svg+xml,' + encodeURIComponent('<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10"><rect width="10" height="10" fill="red"/></svg>');
    } else if (kind === 'audio' || kind === 'video') {
      props.mediaUrl = kind === 'audio' ? '__AUDIO_DATA_URL__' : '__VIDEO_DATA_URL__';
    } else if (kind === 'markdown') {
      props.content = '# Native Markdown\n\n**Rendered content**';
    } else if (kind === 'csv') {
      props.richPayload = { blocks: [{ type: 'table', columns: [{ key: 'name', label: 'Name' }], rows: [{ name: 'Native CSV' }] }] };
    } else if (kind === 'mermaid') {
      props.richPayload = { blocks: [{ type: 'mermaid', source: 'flowchart LR\nA[Native] --> B[Preview]' }] };
    } else {
      props.kind = 'text'; props.filename = 'fixture.json'; props.mime = 'application/json'; props.content = '{"native":true}';
    }
    mounted = mount(ArtifactPreviewContent, { target, props });
    return true;
  },
  close: async () => { await close(); return true; },
} });
