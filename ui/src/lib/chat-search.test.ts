import { describe, expect, it } from 'vitest';
import {
  cleanSearchSnippet,
  findLocalChatMatches,
  findVisibleServerSearchTarget,
  mergeSearchResultsByTarget,
  resultLabel,
  serverSearchFallbackTargetId,
  type ChatSearchResult,
  type SearchTimelineItem
} from '$lib/chat-search';
import type { ConversationFlatSearchMatch } from '$lib/types/api';

function serverMatch(overrides: Partial<ConversationFlatSearchMatch['match']> = {}): ConversationFlatSearchMatch {
  return {
    conversation_id: 'conv-1',
    conversation_title: 'Conversation',
    agent_id: 'agent-1',
    project_id: null,
    status: 'active',
    session_id: 'sess-1',
    intaris_session_id: 'int-1',
    kind_rank: 0,
    match: {
      session_id: 'int-1',
      kind: 'reasoning',
      ref_id: '123',
      role: null,
      ts: null,
      snippet: 'matched phrase',
      score: 1,
      score_breakdown: {},
      agent_id: null,
      session_title: null,
      session_intention: null,
      ...overrides
    }
  };
}

describe('chat search helpers', () => {
  it('finds loaded local message matches', () => {
    const items = [
      {
        id: 'msg-1',
        kind: 'message',
        role: 'user',
        content: 'Find this exact phrase',
        created_at: null
      },
      {
        id: 'tool-1',
        kind: 'tool_call',
        created_at: null
      }
    ] satisfies SearchTimelineItem[];

    const matches = findLocalChatMatches(items, 'exact');

    expect(matches).toHaveLength(1);
    expect(matches[0]?.id).toBe('msg-1');
    expect(matches[0]?.label).toBe('User message');
  });

  it('uses generic labels for server hits', () => {
    const result = {
      source: 'server',
      targetId: 'msg-1',
      server: {
        conversation_id: 'conv-1',
        conversation_title: 'Conversation',
        agent_id: 'agent-1',
        project_id: null,
        status: 'active',
        session_id: 'sess-1',
        intaris_session_id: 'int-1',
        kind_rank: 0,
        match: {
          session_id: 'int-1',
          kind: 'reasoning',
          ref_id: '123',
          role: null,
          ts: null,
          snippet: 'matched <mark>phrase</mark>',
          score: 1,
          score_breakdown: {},
          agent_id: null,
          session_title: null,
          session_intention: null
        }
      }
    } satisfies ChatSearchResult;

    expect(resultLabel(result)).toBe('Search match');
    expect(cleanSearchSnippet(result.server.match.snippet)).toBe('matched phrase');
  });

  it('removes role prefixes from server snippets', () => {
    expect(cleanSearchSnippet('User message: matched <mark>phrase</mark>')).toBe('matched phrase');
    expect(cleanSearchSnippet('Assistant message: matched phrase')).toBe('matched phrase');
  });

  it('deduplicates search hits by target message', () => {
    const server = {
      source: 'server',
      targetId: 'msg-1',
      server: {
        conversation_id: 'conv-1',
        conversation_title: 'Conversation',
        agent_id: 'agent-1',
        project_id: null,
        status: 'active',
        session_id: 'sess-1',
        intaris_session_id: 'int-1',
        kind_rank: 0,
        match: {
          session_id: 'int-1',
          kind: 'reasoning',
          ref_id: '123',
          role: null,
          ts: null,
          snippet: 'matched phrase',
          score: 0.9,
          score_breakdown: {},
          agent_id: null,
          session_title: null,
          session_intention: null
        }
      }
    } satisfies ChatSearchResult;
    const local = {
      source: 'local',
      targetId: 'msg-1',
      local: {
        id: 'msg-1',
        label: 'User message',
        snippet: 'matched phrase'
      }
    } satisfies ChatSearchResult;

    expect(mergeSearchResultsByTarget([server, local])).toEqual([local]);
  });

  it('targets only visible messages that contain the server match text', () => {
    const items = [
      {
        id: 'nearby-message',
        kind: 'message',
        role: 'assistant',
        content: 'Obsidian sync looks healthy.',
        created_at: '2026-01-01T10:00:00Z'
      },
      {
        id: 'actual-message',
        kind: 'message',
        role: 'user',
        content: 'Vis jak jsme resili ten svoz odpadu. Zitra je prvni svoz.',
        created_at: '2026-01-01T09:00:00Z'
      }
    ] satisfies SearchTimelineItem[];

    const match = serverMatch({
      ts: '2026-01-01T10:00:01Z',
      snippet: 'User message: Vis jak jsme resili ten <mark>svoz odpadu</mark>. Zitra je prvni svoz.'
    });

    expect(findVisibleServerSearchTarget(items, match)).toBe('actual-message');
  });

  it('does not fabricate a visible target when the server match is not loaded', () => {
    const items = [
      {
        id: 'wrong-message',
        kind: 'message',
        role: 'assistant',
        content: 'A different currently loaded message also mentions svoz odpadu.',
        created_at: '2026-01-01T10:00:00Z'
      }
    ] satisfies SearchTimelineItem[];

    const match = serverMatch({
      ts: '2026-01-01T10:00:01Z',
      snippet: 'User message: Vis jak jsme resili ten <mark>svoz odpadu</mark>.'
    });

    expect(findVisibleServerSearchTarget(items, match)).toBeNull();
    expect(serverSearchFallbackTargetId(match)).toBe('server:int-1:reasoning:123');
  });

  it('does not target a visible message with the wrong role', () => {
    const items = [
      {
        id: 'assistant-quote',
        kind: 'message',
        role: 'assistant',
        content: 'Vis jak jsme resili ten svoz odpadu. Zitra je prvni svoz.',
        created_at: '2026-01-01T10:00:00Z'
      }
    ] satisfies SearchTimelineItem[];

    const match = serverMatch({
      role: 'user',
      ts: '2026-01-01T10:00:01Z',
      snippet: 'User message: Vis jak jsme resili ten <mark>svoz odpadu</mark>. Zitra je prvni svoz.'
    });

    expect(findVisibleServerSearchTarget(items, match)).toBeNull();
  });
});
