import { readFile } from 'node:fs/promises';
import { resolve } from 'node:path';
import { test, expect } from '@playwright/test';
import { preparePdfTextLayer } from '../src/lib/feedback/annotations/pdf-layer';

function pdfBytes(rotation: number) {
  const stream = 'BT /F1 12 Tf 50 100 Td (Rotated selection) Tj ET';
  const objects = ['<< /Type /Catalog /Pages 2 0 R >>', '<< /Type /Pages /Kids [3 0 R] /Count 1 >>',
    `<< /Type /Page /Parent 2 0 R /MediaBox [0 0 600 800] /Rotate ${rotation} /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>`,
    '<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>', `<< /Length ${stream.length} >>\nstream\n${stream}\nendstream`];
  let body = '%PDF-1.4\n'; const offsets = [0];
  objects.forEach((object, index) => { offsets.push(body.length); body += `${index + 1} 0 obj\n${object}\nendobj\n`; });
  const xref = body.length;
  body += `xref\n0 6\n0000000000 65535 f \n${offsets.slice(1).map(offset => `${String(offset).padStart(10, '0')} 00000 n \n`).join('')}trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n${xref}\n%%EOF`;
  return [...new TextEncoder().encode(body)];
}
// Real PDF.js/browser geometry qualification; not API-backed product acceptance.
for (const rotation of [90, 270]) for (const scale of [0.75, 1.5]) {
  test(`real PDF text layer aligns at ${rotation} degrees and ${scale} scale`, async ({ page }) => {
    const [module, worker, css] = await Promise.all([
      readFile(resolve('node_modules/pdfjs-dist/legacy/build/pdf.mjs')),
      readFile(resolve('node_modules/pdfjs-dist/legacy/build/pdf.worker.mjs')),
      readFile(resolve('node_modules/pdfjs-dist/web/pdf_viewer.css')),
    ]);
    await page.route('https://annotation-pdf.test/**', async route => {
      const path = new URL(route.request().url()).pathname;
      if (path === '/pdf.mjs') return route.fulfill({ contentType: 'text/javascript', body: module });
      if (path === '/worker.mjs') return route.fulfill({ contentType: 'text/javascript', body: worker });
      if (path === '/pdf.css') return route.fulfill({ contentType: 'text/css', body: css });
      if (path === '/') return route.fulfill({ contentType: 'text/html', body: '<link rel="stylesheet" href="/pdf.css"><div id="page" style="position:relative"><div class="textLayer"></div></div>' });
      await route.abort();
    });
    await page.goto('https://annotation-pdf.test/');
    const result = await page.evaluate(async ({ data, scale, prepare }) => {
      const path = '/pdf.mjs';
      const pdfjs = await import(path);
      pdfjs.GlobalWorkerOptions.workerSrc = '/worker.mjs';
      const task = pdfjs.getDocument({ data: new Uint8Array(data), useSystemFonts: true });
      const pdf = await task.promise;
      const pdfPage = await pdf.getPage(1), viewport = pdfPage.getViewport({ scale });
      const parent = document.querySelector<HTMLElement>('#page')!, layer = document.querySelector<HTMLElement>('.textLayer')!;
      parent.style.width = `${viewport.width}px`; parent.style.height = `${viewport.height}px`;
      // Execute the exact self-contained production setup, not a copied CSS fixture.
      const setup = new Function(`return (${prepare})`)() as typeof preparePdfTextLayer;
      setup(layer, viewport.scale, pdfPage.userUnit);
      const textLayer = new pdfjs.TextLayer({ textContentSource: pdfPage.streamTextContent(), container: layer, viewport });
      await textLayer.render(); await document.fonts.ready;
      const bounds = layer.getBoundingClientRect();
      const span = layer.querySelector('span')!;
      const textBounds = span.getBoundingClientRect();
      const range = document.createRange(); range.selectNodeContents(span);
      window.getSelection()!.removeAllRanges(); window.getSelection()!.addRange(range);
      const result = { width: bounds.width, height: bounds.height, expectedWidth: viewport.width, expectedHeight: viewport.height,
        selection: window.getSelection()!.toString(), inside: textBounds.left >= bounds.left - 2 && textBounds.right <= bounds.right + 2
          && textBounds.top >= bounds.top - 2 && textBounds.bottom <= bounds.bottom + 2 };
      await task.destroy(); return result;
    }, { data: pdfBytes(rotation), scale, prepare: preparePdfTextLayer.toString() });
    expect(Math.abs(result.width - result.expectedWidth)).toBeLessThan(2);
    expect(Math.abs(result.height - result.expectedHeight)).toBeLessThan(2);
    expect(result.selection).toBe('Rotated selection');
    expect(result.inside).toBe(true);
  });
}
