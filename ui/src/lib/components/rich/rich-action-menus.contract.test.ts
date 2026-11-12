import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { describe, expect, it } from 'vitest';

const read = (file: string) => readFileSync(resolve('src/lib/components/rich', file), 'utf8');
const deliverable = read('RichDeliverable.svelte');
const share = read('ShareMenu.svelte');
const download = read('DownloadMenu.svelte');
const actions = read('document-actions.css');

describe('rich deliverable action menus', () => {
  it('lets both open menus escape the clipped deliverable card', () => {
    expect(deliverable).toMatch(/overflow: clip;/);
    expect(deliverable).toContain('.rich-deliverable:has(:global(.download-menu[open]))');
    expect(deliverable).toContain('.rich-deliverable:has(:global(.share-menu .share-panel))');
  });

  it('sizes child-component triggers from the host toolbar instead of their own breakpoints', () => {
    expect(deliverable).toContain('--rich-action-size: 2.75rem;');
    expect(deliverable).toContain('--rich-action-size: 2.25rem;');
    for (const source of [share, download]) {
      expect(source).toContain("import './document-actions.css'");
      expect(source).toContain('class="document-icon-action"');
      expect(source).not.toMatch(/width: 44px; height: 44px/);
    }
    expect(actions).toMatch(/width: var\(--rich-action-size,/);
    expect(actions).toMatch(/height: var\(--rich-action-size,/);
  });

  it('opens public-link options in a dialog rather than a clipped inline share menu', () => {
    expect(share).toContain('<PublicLinkDialog');
    expect(share).toContain('aria-haspopup="dialog"');
    expect(read('PublicLinkDialog.svelte')).toContain('<BlockingDialog');
    expect(download).toContain('use:anchoredMenu');
  });
});
