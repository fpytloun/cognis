import { fireEvent, render, screen } from '@testing-library/svelte';
import { describe, expect, it, vi } from 'vitest';

import KnowledgeCard from './KnowledgeCard.svelte';
import type { KnowledgebaseDiagnostics, KnowledgebaseModel } from '$lib/types/api';

const kb: KnowledgebaseModel = {
  knowledgebase_id: 'kb_1',
  owner_email: 'owner@example.com',
  access_level: 'owner',
  name: 'Product docs',
  description: 'Everything about the product',
  status: 'active',
  metadata_schema: {},
  settings: {},
  created_at: '2026-01-01T00:00:00Z',
  updated_at: '2026-01-02T00:00:00Z',
  archived_at: null
};

const diagnostics: KnowledgebaseDiagnostics = {
  enabled: true,
  artifact_counts: { indexed: 5, failed: 1, detached: 2 },
  job_counts: { failed: 1 },
  chunk_count: 42,
  backend_health: {}
};

describe('KnowledgeCard', () => {
  it('renders name, description, counts, and failed-job warning, excluding detached/removed from the document count', () => {
    render(KnowledgeCard, {
      kb,
      diagnostics,
      onOpen: vi.fn(),
      onArchive: vi.fn(),
      onReactivate: vi.fn(),
      onDelete: vi.fn()
    });

    expect(screen.getByText('Product docs')).toBeInTheDocument();
    expect(screen.getByText('Everything about the product')).toBeInTheDocument();
    expect(screen.getByText('6 documents')).toBeInTheDocument();
    expect(screen.getByText('42 chunks')).toBeInTheDocument();
    expect(screen.getByText('1 failed job')).toBeInTheDocument();
  });

  it('invokes onOpen when the card title is activated', async () => {
    const onOpen = vi.fn();
    render(KnowledgeCard, { kb, onOpen, onArchive: vi.fn(), onReactivate: vi.fn(), onDelete: vi.fn() });

    await fireEvent.click(screen.getByTestId('knowledge-card-open'));
    expect(onOpen).toHaveBeenCalledWith(kb);
  });

  it('offers reactivate instead of archive for an archived knowledgebase', () => {
    render(KnowledgeCard, {
      kb: { ...kb, status: 'archived' },
      onOpen: vi.fn(),
      onArchive: vi.fn(),
      onReactivate: vi.fn(),
      onDelete: vi.fn()
    });

    expect(screen.getByRole('button', { name: 'Reactivate' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /Archive/ })).not.toBeInTheDocument();
  });

  it('preserves read access while hiding viewer mutations', () => {
    render(KnowledgeCard, {
      kb,
      canMutate: false,
      onOpen: vi.fn(),
      onArchive: vi.fn(),
      onReactivate: vi.fn(),
      onDelete: vi.fn()
    });

    expect(screen.getByRole('button', { name: 'Open' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /Archive/ })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /Delete/ })).not.toBeInTheDocument();
  });

  it('badges shared access, shows owner identity, and never exposes lifecycle actions', () => {
    render(KnowledgeCard, {
      kb: { ...kb, access_level: 'shared', owner_email: 'alice@example.com' },
      canMutate: true,
      onOpen: vi.fn(), onArchive: vi.fn(), onReactivate: vi.fn(), onDelete: vi.fn()
    });
    expect(screen.getByText('Shared with you')).toBeInTheDocument();
    expect(screen.getByText('Shared by alice@example.com')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /Archive|Delete|Reactivate/ })).not.toBeInTheDocument();
  });
});
