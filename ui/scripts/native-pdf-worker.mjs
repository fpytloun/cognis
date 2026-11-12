// PDF.js 6 loads non-WASM image decoders with a runtime import. WKWebView
// cannot import custom-scheme modules; compile the two shipped decoders into
// the native worker instead. Fail the build on an upstream contract change.
export function nativePdfWorker() {
  return {
    name: 'native-pdf-image-decoders',
    transform(code, id) {
      if (!id.split('?')[0].endsWith('/pdfjs-dist/legacy/build/pdf.worker.mjs')) return;
      const runtimeImport = /import\(\s*\/\*webpackIgnore: true\*\/\s*\/\*@vite-ignore\*\/\s*`\$\{WasmImage\.#wasmUrl\}\$\{this\._noWasmFilename\}`\)/g;
      if ([...code.matchAll(runtimeImport)].length !== 1) {
        throw new Error('PDF.js image-decoder import changed; revalidate native worker packaging');
      }
      return {
        code: 'import openjpegFallback from "pdfjs-dist/wasm/openjpeg_nowasm_fallback.js";\n' +
          'import jbig2Fallback from "pdfjs-dist/wasm/jbig2_nowasm_fallback.js";\n' +
          code.replace(runtimeImport,
            'Promise.resolve({default: ({"openjpeg_nowasm_fallback.js": openjpegFallback, "jbig2_nowasm_fallback.js": jbig2Fallback})[this._noWasmFilename]})'),
        map: null,
      };
    },
  };
}
