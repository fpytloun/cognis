import { render, screen } from '@testing-library/svelte';
import { describe, expect, it, vi } from 'vitest';
import ArtifactPreviewContent from './ArtifactPreviewContent.svelte';
import TextDocument from './TextDocument.svelte';

describe('Shared artifact preview body', () => {
  it.each(['chat', 'library'])('renders canonical CSV payload as inert cells in %s', (surface) => {
    const richPayload = { blocks: [{ type: 'table',
      columns: [{ key: 'value', label: 'Value' }],
      rows: [{ value: { type: 'code', value: '<script>alert(1)</script> =1+1' } }],
    }] };
    const props = { filename: 'data.csv', mime: 'text/csv', richPayload };
    const view = surface === 'chat'
      ? render(ArtifactPreviewContent, { ...props, kind: 'csv', content: 'Value\n=1+1' })
      : render(TextDocument, { ...props, text: 'Value\n=1+1' });
    expect(screen.getByRole('table')).toBeVisible();
    expect(view.container.querySelector('td code')).toHaveTextContent('<script>alert(1)</script> =1+1');
    expect(view.container.querySelector('script')).toBeNull();
  });
  it.each(['audio', 'video'] as const)('renders %s with native controls', (kind) => {
    const { container } = render(ArtifactPreviewContent, {
      kind, filename: `recording.${kind}`, mediaUrl: '/signed-media',
    });
    const media = container.querySelector(kind);
    expect(media).toHaveAttribute('src', '/signed-media');
    expect(media).toHaveAttribute('controls');
  });
  it('refuses bare HTML URLs without a trusted source', () => {
    render(ArtifactPreviewContent, { kind: 'html', filename: 'page.html', mediaUrl: '/signed-html' });
    expect(screen.queryByTitle('Artifact preview')).toBeNull();
    expect(screen.getByText('The document preview could not be loaded.')).toBeVisible();
  });
  it('mounts bounded inline public HTML with only a view guard and tears it down on revocation', async () => {
    const originalURL = globalThis.URL, originalFetch = globalThis.fetch;
    const controller = new AbortController(), fetch = vi.fn(), revoke = vi.fn();
    const guard = { signal: controller.signal, assertCurrent: () => controller.signal.throwIfAborted(),
      validate: vi.fn(async () => controller.signal.throwIfAborted()) };
    vi.stubGlobal('fetch', fetch);
    vi.stubGlobal('URL', class extends URL {
      static createObjectURL = vi.fn(() => 'blob:https://public-view.test/source');
      static revokeObjectURL = revoke;
    });
    const view = render(ArtifactPreviewContent, { kind: 'html', filename: 'public.html', htmlSource: { text: '<p>Public source</p>', guard } });
    try {
      const frame = await screen.findByTitle('Artifact preview');
      expect(frame).toHaveAttribute('sandbox', 'allow-scripts');
      expect(fetch).not.toHaveBeenCalled();
      expect(guard.validate).toHaveBeenCalledTimes(2);
      controller.abort();
      expect(screen.queryByTitle('Artifact preview')).toBeNull();
      expect(revoke).toHaveBeenCalledOnce();
    } finally { view.unmount(); globalThis.URL = originalURL; globalThis.fetch = originalFetch; }
  });
  it('shows truncation warnings and leaves malformed JSON readable', () => {
    const { container } = render(ArtifactPreviewContent, {
      kind: 'text', filename: 'data.json', mime: 'application/json', content: '{"partial":', truncated: true,
    });
    expect(container.querySelector('code')).toHaveTextContent('{"partial":');
    expect(screen.getByText(/Download the complete file/)).toBeVisible();
    expect(container.querySelector('.json-key')).toBeNull();
  });
});
