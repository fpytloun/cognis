import { readFile } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import { transform, Features } from 'lightningcss';

const documentFile = fileURLToPath(new URL('../src/lib/components/rich/RichDeliverable.svelte', import.meta.url));
const tocFile = fileURLToPath(new URL('../src/lib/components/rich/RichToc.svelte', import.meta.url));
const chromeFile = fileURLToPath(new URL('../src/lib/components/rich/RichViewerChrome.svelte', import.meta.url));
const stylesheets = ['rich-blocks.css', 'rich-publication.css', 'rich-conversation.css']
  .map(name => fileURLToPath(new URL(`../src/lib/components/rich/${name}`, import.meta.url)));

/** @param {string | undefined} filename */
export function isRichDocument(filename) {
  return filename?.replaceAll('\\', '/') === documentFile.replaceAll('\\', '/');
}

/** @param {string} filename */
export function usesInjectedRichStyles(filename) {
  const normalized = filename.replaceAll('\\', '/');
  return isRichDocument(filename)
    || normalized === tocFile.replaceAll('\\', '/')
    || normalized === chromeFile.replaceAll('\\', '/');
}

// Svelte installs the canonical cascade synchronously on first use, without
// adding a render-blocking stylesheet to ordinary chat.
/** @type {import('svelte/compiler').PreprocessorGroup} */
export const richDocumentStyles = {
  name: 'cognis-rich-document-styles',
  async style({ content, filename }) {
    if (!isRichDocument(filename)) return;
    const sheets = await Promise.all(stylesheets.map(path => readFile(path, 'utf8')));
    const compiled = transform({
      filename: 'rich-document-styles.css',
      code: Buffer.from(sheets.join('\n')),
      minify: true,
      include: Features.Nesting,
    }).code.toString();
    const global = compiled.replace(/@keyframes\s+([\w-]+)/g, '@keyframes -global-$1');
    return {
      code: `:global {\n${global}\n}\n${content}`,
      dependencies: stylesheets,
    };
  },
};
