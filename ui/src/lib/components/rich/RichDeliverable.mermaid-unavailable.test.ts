import { cleanup, render } from '@testing-library/svelte';
import { afterEach, expect, it, vi } from 'vitest';
import RichDeliverable from './RichDeliverable.svelte';

vi.mock('./mermaid-presentation', () => { throw new Error('Optional presentation unavailable'); });
afterEach(cleanup);

it('retains escaped readable source when the presentation import rejects', async () => {
  const source = 'graph TD; A["<script>unsafe()</script>"] --> B;';
  const view = render(RichDeliverable, {
    payload: { blocks: [{ type: 'mermaid', source }] }, surface: 'embedded', audioEnabled: false,
  });
  await vi.dynamicImportSettled();
  expect(view.container.querySelector('pre[data-mermaid-source]')?.textContent).toBe(source);
  expect(view.container.querySelector('script, .rich-mermaid')).toBeNull();
});
