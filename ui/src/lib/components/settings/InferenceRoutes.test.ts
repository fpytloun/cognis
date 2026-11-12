import { fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import InferenceRoutes from './InferenceRoutes.svelte';
import type { LLMProvider } from '$lib/types/api';

const routes = vi.hoisted(() => ({
  list: vi.fn(), create: vi.fn(), update: vi.fn(), remove: vi.fn()
}));
vi.mock('$lib/api/client', () => ({ api: { inferenceRoutes: routes } }));
const providers = [{
  provider_id: 'anthropic-demo', display_name: 'Anthropic demo', config: {},
  models: [{ model_id: 'claude-opus-5-5' }, { model_id: 'claude-sonnet-4-5' }]
}, {
  provider_id: 'openai', display_name: 'OpenAI', config: {},
  models: [{ model_id: 'gpt-4o' }]
}] as LLMProvider[];

beforeEach(() => {
  vi.stubGlobal('ResizeObserver', class {
    observe() {}
    disconnect() {}
  });
  vi.clearAllMocks();
  routes.list.mockResolvedValue({ routes: [] });
  routes.create.mockResolvedValue({});
});
afterEach(() => vi.unstubAllGlobals());

describe('route target editor', () => {
  it('edits a reactive saved route without mutating the original and saves its revision', async () => {
    const route = {
      route_id: 'saved-route', revision: 3, reference: 'cognis/chat',
      config: { name: 'chat', targets: [
        { provider_id: 'anthropic-demo', model: 'claude-opus-5-5', priority: 0 }
      ] }
    };
    routes.list.mockResolvedValue({ routes: [route] });
    render(InferenceRoutes, { providers });
    await fireEvent.click(await screen.findByRole('button', { name: 'Edit' }));
    expect(screen.getByLabelText('Route name')).toHaveValue('chat');
    await fireEvent.input(screen.getByLabelText('Route name'), { target: { value: 'updated' } });
    expect(route.config.name).toBe('chat');
    await fireEvent.click(screen.getByRole('button', { name: 'Save route' }));
    await waitFor(() => expect(routes.update).toHaveBeenCalledWith(
      expect.objectContaining({ route_id: 'saved-route', revision: 3 }),
      expect.objectContaining({ name: 'updated' })
    ));
  });

  it('portals the picker outside cards, keeps inside clicks open, and cleans up on close', async () => {
    const { container } = render(InferenceRoutes, { providers });
    await fireEvent.click(screen.getByRole('button', { name: 'Add route' }));
    await fireEvent.click(screen.getByRole('button', { name: 'Target 1' }));
    const search = screen.getByRole('combobox');
    const panel = search.parentElement!;
    expect(panel.parentElement).toBe(document.body);
    expect(container.contains(panel)).toBe(false);
    expect(panel).toHaveClass('fixed');
    await fireEvent.pointerDown(search);
    expect(screen.getByRole('combobox')).toBeInTheDocument();
    await fireEvent.pointerDown(document.body);
    expect(screen.queryByRole('combobox')).not.toBeInTheDocument();
    expect(panel.isConnected).toBe(false);
  });

  it('anchors the overlay to the trigger and follows scrolling containers', async () => {
    render(InferenceRoutes, { providers });
    await fireEvent.click(screen.getByRole('button', { name: 'Add route' }));
    const trigger = screen.getByRole('button', { name: 'Target 1' });
    let top = 100;
    vi.spyOn(trigger, 'getBoundingClientRect').mockImplementation(() => ({
      left: 20, right: 420, top, bottom: top + 40, width: 400, height: 40,
      x: 20, y: top, toJSON: () => ({})
    }));
    await fireEvent.click(trigger);
    const panel = screen.getByRole('combobox').parentElement!;
    expect(panel.style.left).toBe('20px');
    expect(panel.style.top).toBe('144px');
    expect(panel.style.width).toBe('400px');
    top = 50;
    await fireEvent.scroll(trigger.parentElement!);
    expect(panel.style.top).toBe('94px');
  });

  it('filters by provider/model, selects with keyboard, autofills and saves split identifiers', async () => {
    render(InferenceRoutes, { providers });
    await fireEvent.click(screen.getByRole('button', { name: 'Add route' }));
    await fireEvent.click(screen.getByRole('button', { name: 'Target 1' }));
    const search = screen.getByRole('combobox');
    await fireEvent.input(search, { target: { value: 'opus' } });
    expect(screen.getAllByRole('option')).toHaveLength(1);
    await fireEvent.keyDown(search, { key: 'Enter' });
    expect(screen.getByLabelText('Route name')).toHaveValue('claude-opus-5-5');
    await fireEvent.click(screen.getByRole('button', { name: 'Save route' }));
    await waitFor(() => expect(routes.create).toHaveBeenCalledWith(expect.objectContaining({
      name: 'claude-opus-5-5',
      targets: [{ provider_id: 'anthropic-demo', model: 'claude-opus-5-5', priority: 0 }]
    })));
  });

  it('preserves a supplied name and supports empty search results and Escape', async () => {
    render(InferenceRoutes, { providers });
    await fireEvent.click(screen.getByRole('button', { name: 'Add route' }));
    await fireEvent.input(screen.getByLabelText('Route name'), { target: { value: 'my-route' } });
    await fireEvent.click(screen.getByRole('button', { name: 'Target 1' }));
    await fireEvent.input(screen.getByRole('combobox'), { target: { value: 'no-match' } });
    expect(screen.getByRole('status')).toHaveTextContent('No matching models');
    await fireEvent.keyDown(screen.getByRole('combobox'), { key: 'Escape' });
    expect(screen.queryByRole('combobox')).not.toBeInTheDocument();
    await fireEvent.click(screen.getByRole('button', { name: 'Target 1' }));
    await fireEvent.input(screen.getByRole('combobox'), { target: { value: 'openai' } });
    await fireEvent.click(screen.getByRole('option'));
    expect(screen.getByLabelText('Route name')).toHaveValue('my-route');
  });

  it('does not use later targets to name a route and rejects missing targets', async () => {
    render(InferenceRoutes, { providers });
    await fireEvent.click(screen.getByRole('button', { name: 'Add route' }));
    await fireEvent.click(screen.getByRole('button', { name: 'Add target' }));
    await fireEvent.click(screen.getByRole('button', { name: 'Target 2' }));
    await fireEvent.click(screen.getAllByRole('option')[0]);
    expect(screen.getByLabelText('Route name')).toHaveValue('');
    await fireEvent.input(screen.getByLabelText('Route name'), { target: { value: 'incomplete' } });
    await fireEvent.submit(screen.getByRole('button', { name: 'Save route' }).closest('form')!);
    expect(routes.create).not.toHaveBeenCalled();
    expect(screen.getByRole('alert')).toHaveTextContent('Choose a model for every target');
  });
});
