import { fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { api } from '$lib/api/client';
import type { KnowledgebaseModel } from '$lib/types/api';
import KnowledgePeopleAccess from './KnowledgePeopleAccess.svelte';

vi.mock('$lib/stores/confirm', () => ({ confirmAction: vi.fn().mockResolvedValue(true) }));

const kb: KnowledgebaseModel = {
  knowledgebase_id: 'kb_1', owner_email: 'owner@example.com', access_level: 'owner',
  name: 'Docs', description: null, status: 'active', metadata_schema: {}, settings: {},
  created_at: null, updated_at: null, archived_at: null
};

describe('KnowledgePeopleAccess', () => {
  afterEach(() => vi.restoreAllMocks());

  it('gates candidate search at two characters and grants access', async () => {
    vi.spyOn(api.knowledgebases, 'shares').mockResolvedValue([]);
    const search = vi.spyOn(api.knowledgebases, 'shareCandidates').mockResolvedValue([
      { email: 'reader@example.com', name: 'Reader' }
    ]);
    vi.spyOn(api.knowledgebases, 'grantShare').mockResolvedValue({
      grant_id: 'kbg_1', user_email: 'reader@example.com', user_name: 'Reader',
      permission: 'view', granted_at: '2026-01-01T00:00:00Z', note: null
    });
    render(KnowledgePeopleAccess, { kb });
    await waitFor(() => expect(api.knowledgebases.shares).toHaveBeenCalled());
    const input = screen.getByTestId('knowledge-share-search');
    await fireEvent.input(input, { target: { value: 'r' } });
    expect(search).not.toHaveBeenCalled();
    await fireEvent.input(input, { target: { value: 're' } });
    await waitFor(() => expect(search).toHaveBeenCalledWith('kb_1', 're', expect.objectContaining({ signal: expect.any(AbortSignal) })));
    await fireEvent.click(await screen.findByRole('button', { name: 'Grant access to Reader' }));
    expect(await screen.findByText('reader@example.com · Read/query')).toBeInTheDocument();
  });

  it('revokes an active share after confirmation', async () => {
    vi.spyOn(api.knowledgebases, 'shares').mockResolvedValue([{
      grant_id: 'kbg_1', user_email: 'reader@example.com', user_name: 'Reader',
      permission: 'view', granted_at: '2026-01-01T00:00:00Z', note: null
    }]);
    const revoke = vi.spyOn(api.knowledgebases, 'revokeShare').mockResolvedValue({ revoked: true });
    render(KnowledgePeopleAccess, { kb });
    await fireEvent.click(await screen.findByRole('button', { name: 'Revoke access from Reader' }));
    await waitFor(() => expect(revoke).toHaveBeenCalledWith('kb_1', 'reader@example.com'));
    expect(screen.getByText('Not shared with anyone yet.')).toBeInTheDocument();
  });

  it('disables search and revocation for archived knowledgebases', async () => {
    vi.spyOn(api.knowledgebases, 'shares').mockResolvedValue([{
      grant_id: 'kbg_1', user_email: 'reader@example.com', user_name: null,
      permission: 'view', granted_at: '2026-01-01T00:00:00Z', note: null
    }]);
    render(KnowledgePeopleAccess, { kb: { ...kb, status: 'archived' }, disabled: true });
    expect(await screen.findByText(/unavailable while this knowledgebase is archived/i)).toBeInTheDocument();
    expect(screen.queryByTestId('knowledge-share-search')).not.toBeInTheDocument();
    expect(await screen.findByRole('button', { name: 'Revoke access from reader@example.com' })).toBeDisabled();
  });

  it('surfaces candidate search errors without losing current shares', async () => {
    vi.spyOn(api.knowledgebases, 'shares').mockResolvedValue([{
      grant_id: 'kbg_1', user_email: 'existing@example.com', user_name: 'Existing',
      permission: 'view', granted_at: '2026-01-01T00:00:00Z', note: null
    }]);
    vi.spyOn(api.knowledgebases, 'shareCandidates').mockRejectedValue(new Error('Search unavailable'));
    render(KnowledgePeopleAccess, { kb });
    await fireEvent.input(screen.getByTestId('knowledge-share-search'), { target: { value: 'zz' } });
    expect(await screen.findByRole('alert')).toHaveTextContent('Search unavailable');
    expect(screen.getByText('existing@example.com · Read/query')).toBeInTheDocument();
  });

  it('aborts and invalidates a delayed search when the query is cleared', async () => {
    vi.spyOn(api.knowledgebases, 'shares').mockResolvedValue([]);
    let resolveSearch!: (value: { email: string; name: string | null }[]) => void;
    const delayed = new Promise<{ email: string; name: string | null }[]>((resolve) => {
      resolveSearch = resolve;
    });
    const search = vi.spyOn(api.knowledgebases, 'shareCandidates').mockReturnValue(delayed);
    render(KnowledgePeopleAccess, { kb });
    const input = screen.getByTestId('knowledge-share-search');
    await fireEvent.input(input, { target: { value: 're' } });
    await waitFor(() => expect(search).toHaveBeenCalled());
    const signal = search.mock.calls[0]?.[2]?.signal;
    await fireEvent.input(input, { target: { value: '' } });
    expect(signal?.aborted).toBe(true);
    resolveSearch([{ email: 'stale@example.com', name: 'Stale' }]);
    await Promise.resolve();
    expect(screen.queryByText('stale@example.com')).not.toBeInTheDocument();
  });
});
