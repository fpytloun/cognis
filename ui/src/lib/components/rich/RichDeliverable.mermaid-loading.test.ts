import { cleanup, render, waitFor } from '@testing-library/svelte';
import { afterEach, expect, it, vi } from 'vitest';
import RichDeliverable from './RichDeliverable.svelte';

const deferred = vi.hoisted(() => {
  let release!: () => void;
  const gate = new Promise<void>((resolve) => release = resolve);
  return { gate, release, evaluated: vi.fn(), render: vi.fn() };
});
vi.mock('./mermaid-presentation', async () => {
  deferred.evaluated();
  await deferred.gate;
  return { renderMermaidPresentation: deferred.render };
});
afterEach(cleanup);

it('does not load without nodes or invoke presentation after disposal during the lazy import', async () => {
  const view = render(RichDeliverable, {
    payload: { blocks: [{ type: 'markdown', content: 'Plain document' }] },
    surface: 'embedded', audioEnabled: false,
  });
  await vi.dynamicImportSettled();
  expect(deferred.evaluated).not.toHaveBeenCalled();
  await view.rerender({ payload: { blocks: [{ type: 'mermaid', source: 'graph TD; A --> B;' }] } });
  await waitFor(() => expect(deferred.evaluated).toHaveBeenCalledOnce());
  view.unmount();
  deferred.release();
  await vi.dynamicImportSettled();
  expect(deferred.render).not.toHaveBeenCalled();
});
