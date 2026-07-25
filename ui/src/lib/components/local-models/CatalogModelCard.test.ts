import { fireEvent, render, screen } from '@testing-library/svelte';
import { describe, expect, it, vi } from 'vitest';

import type { LocalModelCatalogItem } from '$lib/types/api';
import CatalogModelCard from './CatalogModelCard.svelte';

const model: LocalModelCatalogItem = {
  catalog_id: 'huggingface:acme/model-GGUF',
  source: 'huggingface',
  requested_ref: 'hf.co/acme/model-GGUF:Q4_K_M',
  title: 'model-GGUF',
  publisher: 'acme',
  repository_url: 'https://huggingface.co/acme/model-GGUF',
  model_card_url: 'https://huggingface.co/acme/model-GGUF/blob/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/README.md',
  revision_sha: 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
  license: 'apache-2.0',
  description: 'A useful local model.',
  downloads: 1200,
  likes: 42,
  last_modified: '2026-07-01T00:00:00Z',
  pipeline_tag: 'text-generation',
  tags: ['gguf'],
  base_models: ['acme/base'],
  capabilities: ['chat'],
  parameter_count: 8_000_000_000,
  quantizations: [
    {
      name: 'Q4_K_M',
      requested_ref: 'hf.co/acme/model-GGUF:Q4_K_M',
      file_name: 'model-Q4_K_M.gguf',
      size_bytes: 5_000_000_000,
      bits_per_weight: 4.5
    }
  ],
  file_size_bytes: 5_000_000_000,
  advertised_max_context: 32768,
  architecture: {},
  architecture_name: 'LlamaForCausalLM',
  metadata_status: 'complete',
  metadata_confidence: 'high',
  metadata_diagnostics: [],
  reference_integrity: 'floating',
  warnings: []
};

describe('CatalogModelCard', () => {
  it('renders useful metrics and accessible external links without a floating warning', async () => {
    const onselect = vi.fn();
    render(CatalogModelCard, { model, onselect });

    const repository = screen.getByRole('link', { name: /open model-GGUF repository/i });
    const card = screen.getByRole('link', { name: /open model-GGUF model card/i });
    expect(repository).toHaveAttribute('target', '_blank');
    expect(repository).toHaveAttribute('rel', 'noreferrer');
    expect(card).toHaveAttribute('href', model.model_card_url);
    expect(screen.getByText('1,200 downloads')).toBeInTheDocument();
    expect(screen.getByText('42 likes')).toBeInTheDocument();
    expect(screen.queryByText(/not pinned|floating reference/i)).not.toBeInTheDocument();

    await fireEvent.click(screen.getByRole('button', { name: 'Plan deployment' }));
    expect(onselect).toHaveBeenCalledWith(model, model.requested_ref);
  });
});
