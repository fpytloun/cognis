import { describe, expect, it } from 'vitest';
import { findLocalChatMatches, resultLabel, stripMarks, type ChatSearchResult } from '$lib/chat-search';
import type { TimelineItem } from '$lib/chat';

describe('chat search helpers', () => {
  it('finds loaded local message matches', () => {
    const items = [
      {
        id: 'msg-1',
        kind: 'message',
        role: 'user',
        content: 'Find this exact phrase',
        html: '',
        seq: 1,
        timestamp: null
      },
      {
        id: 'tool-1',
        kind: 'tool_call',
        callId: 'call-1',
        toolName: 'noop',
        status: 'completed',
        timestamp: null
      }
    ] satisfies TimelineItem[];

    const matches = findLocalChatMatches(items, 'exact');

    expect(matches).toHaveLength(1);
    expect(matches[0]?.id).toBe('msg-1');
    expect(matches[0]?.label).toBe('User message');
  });

  it('labels reasoning server hits as the precise result kind', () => {
    const result = {
      source: 'server',
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

    expect(resultLabel(result)).toBe('Reasoning');
    expect(stripMarks(result.server.match.snippet)).toBe('matched phrase');
  });
});
