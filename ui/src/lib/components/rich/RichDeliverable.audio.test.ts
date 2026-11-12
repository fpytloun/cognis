import { fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import { expect, it, vi } from 'vitest';
import RichDeliverable from './RichDeliverable.svelte';

const gate = vi.hoisted(() => {
  let resolve!: (value: typeof import('../DeliverableAudio.svelte')) => void;
  const promise = new Promise<typeof import('../DeliverableAudio.svelte')>(next => { resolve = next; });
  return { promise, resolve };
});
vi.mock('../DeliverableAudio.svelte', async () => {
  const module = await gate.promise;
  // Native module namespaces allow unknown symbol reads. Preserve that behavior
  // when Svelte dev tracing probes a Vitest mock stored in raw reactive state.
  return new Proxy(module, {
    has: (target, key) => typeof key === 'symbol' || Reflect.has(target, key),
  });
});
// Compilation is outside the readiness test; the mock gate owns availability.
const actualAudio = await vi.importActual<typeof import('../DeliverableAudio.svelte')>('../DeliverableAudio.svelte');

it('renders the styled body while fallback audio is pending and preserves the portable client', async () => {
  const source = 'dlv_11111111111111111111111111111111';
  const client = {
    context: vi.fn().mockResolvedValue({
      supported: false, can_generate: false, can_cancel: false, detail: null, jobs: [],
    }),
    estimates: vi.fn(), create: vi.fn(), get: vi.fn(), cancel: vi.fn(),
  };
  render(RichDeliverable, {
    surface: 'standalone', instanceId: source, title: 'Ready document', audioClient: client,
    payload: { version: 1, blocks: [{ type: 'markdown', content: 'Immediate document body.' }] },
  });
  const body = screen.getByTestId('rich-deliverable-body');
  expect(body).toHaveTextContent('Immediate document body.');
  expect(screen.getByText('Loading audio controls…')).toBeInTheDocument();
  expect(client.context).not.toHaveBeenCalled();
  gate.resolve({ default: actualAudio.default });
  await waitFor(() => expect(screen.queryByText('Loading audio controls…')).toBeNull());
  const listen = await screen.findByRole('button', { name: 'Listen to this document' });
  expect(screen.getByTestId('rich-deliverable-body')).toBe(body);
  await fireEvent.click(listen);
  await waitFor(() => expect(client.context).toHaveBeenCalledWith(source, expect.objectContaining({
    signal: expect.any(AbortSignal),
  })));
  expect(client.create).not.toHaveBeenCalled();
});
