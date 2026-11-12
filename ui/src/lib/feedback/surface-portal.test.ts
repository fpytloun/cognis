import { waitFor } from '@testing-library/svelte';
import { expect, it, vi } from 'vitest';
import { surfacePortal } from './surface-portal';

it('tracks retained owner visibility and releases its observer and owner anchor', async () => {
  const owner = document.createElement('section'), node = document.createElement('div');
  document.body.append(owner); owner.append(node);
  const active = vi.fn(), mounted = surfacePortal(node, active)!;
  try {
    expect(node.parentElement).toBe(document.body);
    expect(active).toHaveBeenLastCalledWith(true);
    owner.hidden = true;
    await waitFor(() => expect(active).toHaveBeenLastCalledWith(false));
    owner.hidden = false;
    await waitFor(() => expect(active).toHaveBeenLastCalledWith(true));
    owner.setAttribute('inert', '');
    await waitFor(() => expect(active).toHaveBeenLastCalledWith(false));
    owner.removeAttribute('inert');
    await waitFor(() => expect(active).toHaveBeenLastCalledWith(true));
    owner.setAttribute('aria-hidden', 'true');
    await waitFor(() => expect(active).toHaveBeenLastCalledWith(false));
    owner.removeAttribute('aria-hidden');
    await waitFor(() => expect(active).toHaveBeenLastCalledWith(true));
    owner.style.display = 'none';
    await waitFor(() => expect(active).toHaveBeenLastCalledWith(false));
    owner.style.display = '';
    await waitFor(() => expect(active).toHaveBeenLastCalledWith(true));
    owner.style.visibility = 'hidden';
    await waitFor(() => expect(active).toHaveBeenLastCalledWith(false));
    owner.style.visibility = '';
    await waitFor(() => expect(active).toHaveBeenLastCalledWith(true));
    mounted.destroy();
    expect(node.isConnected).toBe(false);
    expect(owner.childNodes).toHaveLength(0);
    const calls = active.mock.calls.length; owner.hidden = true; await Promise.resolve();
    expect(active).toHaveBeenCalledTimes(calls);
  } finally { mounted.destroy(); owner.remove(); }
});
