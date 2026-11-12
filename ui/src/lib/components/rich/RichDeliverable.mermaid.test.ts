import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import { tick } from 'svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import RichDeliverable from './RichDeliverable.svelte';

const engine = vi.hoisted(() => ({ initialize: vi.fn(), render: vi.fn() }));
vi.mock('mermaid', () => ({ default: engine }));

const source = 'graph TD; A --> B;';
const nextSource = 'graph TD; C --> D;';
const payload = (code = source) => ({ blocks: [{ type: 'mermaid', source: code }] });
const svg = (label = 'Diagram') => `<svg xmlns="http://www.w3.org/2000/svg" aria-labelledby="label"><title id="label">${label}</title><path id="edge" marker-end="url(#arrow)" /><marker id="arrow" /></svg>`;
function deferred() {
  let resolve!: (result: { svg: string }) => void;
  const promise = new Promise<{ svg: string }>((done) => resolve = done);
  return { promise, resolve };
}
const mount = () => {
  const container = document.createElement('div');
  document.body.append(container);
  return render(RichDeliverable, { target: container, props: { payload: payload(), surface: 'embedded', audioEnabled: false } });
};
async function settle() {
  await vi.dynamicImportSettled();
  await tick();
}

beforeEach(() => {
  document.documentElement.setAttribute('data-resolved-theme', 'dark');
  engine.initialize.mockReset();
  engine.render.mockReset().mockResolvedValue({ svg: svg() });
});
afterEach(() => {
  cleanup();
  document.documentElement.removeAttribute('data-resolved-theme');
});

describe('RichDeliverable Mermaid publication ownership', () => {
  it('does no Mermaid work without source nodes', async () => {
    render(RichDeliverable, { payload: { blocks: [{ type: 'markdown', content: 'Readable document' }] }, surface: 'embedded', audioEnabled: false });
    await settle();
    expect(engine.initialize).not.toHaveBeenCalled();
    expect(engine.render).not.toHaveBeenCalled();
  });

  it('keeps strict initialization and isolated accessible SVG references across viewers', async () => {
    const first = mount();
    await waitFor(() => expect(first.container.querySelector('.rich-mermaid svg')).not.toBeNull());
    const second = mount();
    await waitFor(() => expect(document.querySelectorAll('.rich-mermaid svg'), JSON.stringify({
      calls: engine.render.mock.calls,
      viewers: [first.container, second.container].map((container) => ({
        connected: container.isConnected,
        source: container.querySelector('pre[data-mermaid-source]')?.textContent,
        svg: container.querySelector('.rich-mermaid')?.innerHTML,
      })),
    })).toHaveLength(2));
    expect(engine.initialize).toHaveBeenCalledWith(expect.objectContaining({ securityLevel: 'strict', startOnLoad: false }));
    const diagrams = [first.container, second.container].map((container) => container.querySelector('svg[aria-labelledby]')!);
    const ids = diagrams.map((diagram) => diagram.getAttribute('aria-labelledby'));
    expect(ids[0]).not.toBe(ids[1]);
    for (const diagram of diagrams) {
      expect(diagram.querySelector(`[id="${diagram.getAttribute('aria-labelledby')}"]`)).not.toBeNull();
      expect(diagram.querySelector('path')?.getAttribute('marker-end')).toMatch(/^url\(#.+-arrow\)$/);
    }
  });

  it.each(['render', 'invalid XML'])('retains readable escaped source on %s failure', async (failure) => {
    if (failure === 'render') engine.render.mockRejectedValue(new Error('Unavailable renderer'));
    else engine.render.mockResolvedValue({ svg: '<svg><broken></svg>' });
    const view = mount();
    await waitFor(() => expect(engine.render).toHaveBeenCalled());
    await settle();
    expect(view.container.querySelector('pre[data-mermaid-source]')?.textContent).toBe(source);
    expect(view.container.querySelector('.rich-mermaid')).toBeNull();
  });

  it('does not publish a render completed after disposal', async () => {
    const pending = deferred();
    engine.render.mockReturnValueOnce(pending.promise);
    const view = mount();
    await waitFor(() => expect(engine.render).toHaveBeenCalledOnce());
    const node = view.container.querySelector('pre[data-mermaid-source]')!;
    const replace = vi.spyOn(node, 'replaceWith');
    view.unmount();
    pending.resolve({ svg: svg('Stale') });
    await settle();
    expect(replace).not.toHaveBeenCalled();
  });

  it('does not publish stale content and serially renders its replacement', async () => {
    const pending = deferred();
    engine.render.mockReturnValueOnce(pending.promise).mockResolvedValue({ svg: svg('Current') });
    const view = mount();
    await waitFor(() => expect(engine.render).toHaveBeenCalledOnce());
    const oldNode = view.container.querySelector('pre[data-mermaid-source]')!;
    const replace = vi.spyOn(oldNode, 'replaceWith');
    await view.rerender({ payload: payload(nextSource) });
    expect(engine.render).toHaveBeenCalledOnce();
    pending.resolve({ svg: svg('Stale') });
    await waitFor(() => expect(view.container.querySelector('.rich-mermaid')?.textContent).toContain('Current'));
    expect(replace.mock.calls.some(([node]) => node instanceof HTMLElement && node.textContent?.includes('Stale'))).toBe(false);
    expect(engine.render.mock.calls.at(-1)?.[1]).toBe(nextSource);
  });

  it('invalidates an in-flight render when its resolved theme changes', async () => {
    const pending = deferred();
    engine.render.mockReturnValueOnce(pending.promise).mockResolvedValue({ svg: svg('Current theme') });
    const view = mount();
    await waitFor(() => expect(engine.render).toHaveBeenCalledOnce());
    const node = view.container.querySelector('pre[data-mermaid-source]')!;
    const replace = vi.spyOn(node, 'replaceWith');
    document.documentElement.setAttribute('data-resolved-theme', 'light');
    await tick();
    await Promise.resolve();
    pending.resolve({ svg: svg('Stale theme') });
    await waitFor(() => expect(view.container.querySelector('.rich-mermaid')?.textContent).toContain('Current theme'));
    expect(replace.mock.calls.some(([node]) => node instanceof HTMLElement && node.textContent?.includes('Stale'))).toBe(false);
    document.documentElement.removeAttribute('data-resolved-theme');
  });

  it('does not publish a modal render completed after the modal closes', async () => {
    const view = mount();
    await waitFor(() => expect(view.container.querySelector('.rich-mermaid')).not.toBeNull());
    const pending = deferred();
    engine.render.mockReturnValueOnce(pending.promise);
    await fireEvent.click(screen.getAllByRole('button', { name: 'Open fullscreen' })[0]);
    await waitFor(() => expect(document.querySelector('[data-testid="rich-deliverable-full-view"] pre[data-mermaid-source]')).not.toBeNull());
    await waitFor(() => expect(engine.render).toHaveBeenCalledTimes(2));
    const modalNode = document.querySelector('[data-testid="rich-deliverable-full-view"] pre[data-mermaid-source]')!;
    const replace = vi.spyOn(modalNode, 'replaceWith');
    await fireEvent.click(screen.getByRole('button', { name: /^Close$/ }));
    pending.resolve({ svg: svg('Closed modal') });
    await settle();
    expect(replace).not.toHaveBeenCalled();
    expect(document.querySelector('[data-testid="rich-deliverable-full-view"]')).toBeNull();
  });
});
