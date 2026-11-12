// @vitest-environment node
import { fileURLToPath } from 'node:url';
import { compile, preprocess } from 'svelte/compiler';
import { describe, expect, it } from 'vitest';
import { isRichDocument, richDocumentStyles, usesInjectedRichStyles } from '../../build-plugins/rich-document-styles.js';

const filename = fileURLToPath(new URL('./components/rich/RichDeliverable.svelte', import.meta.url));

describe('synchronous canonical rich styles', () => {
  it.each(['client', 'server'] as const)('embeds the canonical cascade in %s rendering', async generate => {
    const processed = await preprocess(
      '<section class="rich-deliverable">Ready</section><style>.rich-deliverable { color: red; }</style>',
      richDocumentStyles, { filename },
    );
    const result = compile(processed.code, { filename, generate, css: 'injected' });
    expect(result.css).toBeNull();
    expect(result.js.code).toContain('--rich-density-gap');
    expect(result.js.code).not.toContain('-global-');
    expect(processed.dependencies).toHaveLength(3);
    // Font URLs must go through Vite's asset pipeline, not a document-relative
    // URL inside an injected style element.
    expect(processed.code).not.toContain('./fonts/');
  });

  it('leaves unrelated components and styles untouched', async () => {
    expect(isRichDocument(filename)).toBe(true);
    expect(usesInjectedRichStyles(filename)).toBe(true);
    const other = fileURLToPath(new URL('./components/ChatMessage.svelte', import.meta.url));
    expect(usesInjectedRichStyles(other)).toBe(false);
    const source = '<p>Ordinary message</p><style>p { color: red; }</style>';
    expect((await preprocess(source, richDocumentStyles, { filename: other })).code).toBe(source);
  });
});
