import { describe, expect, it } from 'vitest';
import type { ActivityOverviewResponse } from '$lib/chat-v2/types';
import {
  activityOverviewHasContent,
  activityOverviewReadPresentation,
  beginActivityOverviewRead,
  cancelActivityOverviewRead,
  classifyActivityOverviewPresentation,
  emptyActivityOverviewReadState,
  settleActivityOverviewRead,
} from './activityOverviewState';

function overview(state: 'live' | 'catching_up' | 'partial' | 'failed', changedFiles = 0): ActivityOverviewResponse {
  return {
    schema_version: 2,
    projection_version: 'live-v1',
    scope: { key: 'conversation:c1', kind: 'conversation', conversation_id: 'c1' },
    summary: { changed_files: changedFiles, commands: 0, mutations: 0, artifacts: 0 },
    materialization: { state },
    workstreams: [],
    recent: {},
    graph_fingerprint: 'graph',
    graph_truncated: false,
  };
}

describe('activityOverviewState', () => {
  it('classifies empty live data as no activity', () => {
    const value = overview('live');
    expect(activityOverviewHasContent(value)).toBe(false);
    expect(classifyActivityOverviewPresentation(value)).toMatchObject({
      hasContent: false,
      lifecycle: { kind: 'none' },
    });
  });

  it.each(['catching_up', 'partial', 'failed'] as const)('retains content for %s', (state) => {
    expect(classifyActivityOverviewPresentation(overview(state, 2))).toMatchObject({
      hasContent: true,
    });
  });
});

describe('activity overview read state', () => {
  it('shows loading, not an error, during the first root read', () => {
    const started = beginActivityOverviewRead(
      emptyActivityOverviewReadState(),
      'root',
      'conversation:root',
      false,
    );
    expect(activityOverviewReadPresentation(started.state, [{
      section: 'root',
      scopeKey: 'conversation:root',
      hasData: false,
    }])).toEqual({ loading: true, refreshing: false, ready: false, error: null });
  });

  it('retains same-scope content while refreshing and reports actual failure only', () => {
    const started = beginActivityOverviewRead(
      emptyActivityOverviewReadState(),
      'focused',
      'session:child',
      true,
    );
    expect(activityOverviewReadPresentation(started.state, [{
      section: 'focused',
      scopeKey: 'session:child',
      hasData: true,
    }])).toMatchObject({ loading: false, refreshing: true, ready: true, error: null });
    const failed = settleActivityOverviewRead(started.state, started.token, {
      applied: false,
      hasData: true,
      error: 'network failed',
    });
    expect(activityOverviewReadPresentation(failed, [{
      section: 'focused',
      scopeKey: 'session:child',
      hasData: true,
    }])).toMatchObject({ ready: true, error: null });
  });

  it('does not expose data or failures from a previous scope', () => {
    const started = beginActivityOverviewRead(
      emptyActivityOverviewReadState(),
      'focused',
      'session:old',
      true,
    );
    const failed = settleActivityOverviewRead(started.state, started.token, {
      applied: false,
      hasData: true,
      error: 'old failure',
    });
    expect(activityOverviewReadPresentation(failed, [{
      section: 'focused',
      scopeKey: 'session:new',
      hasData: false,
    }])).toEqual({ loading: false, refreshing: false, ready: false, error: null });
  });

  it('rejects completion after cancellation', () => {
    const started = beginActivityOverviewRead(
      emptyActivityOverviewReadState(),
      'root',
      'conversation:root',
      false,
    );
    const cancelled = cancelActivityOverviewRead(started.state, 'root');
    expect(settleActivityOverviewRead(cancelled, started.token, {
      applied: true,
      hasData: true,
    })).toBe(cancelled);
  });

  it.each(['root', 'focused'] as const)(
    'waits for both child sources when %s finishes first',
    (firstSection) => {
      const root = beginActivityOverviewRead(
        emptyActivityOverviewReadState(),
        'root',
        'conversation:root',
        false,
      );
      const focused = beginActivityOverviewRead(
        root.state,
        'focused',
        'session:child',
        false,
      );
      const firstToken = firstSection === 'root' ? root.token : focused.token;
      const afterFirst = settleActivityOverviewRead(focused.state, firstToken, {
        applied: true,
        hasData: true,
      });
      const requirements = [
        { section: 'root' as const, scopeKey: 'conversation:root', hasData: firstSection === 'root' },
        { section: 'focused' as const, scopeKey: 'session:child', hasData: firstSection === 'focused' },
      ];
      expect(activityOverviewReadPresentation(afterFirst, requirements)).toMatchObject({
        loading: true,
        ready: false,
        error: null,
      });
      const secondToken = firstSection === 'root' ? focused.token : root.token;
      const complete = settleActivityOverviewRead(afterFirst, secondToken, {
        applied: true,
        hasData: true,
      });
      expect(activityOverviewReadPresentation(complete, requirements.map((item) => ({
        ...item,
        hasData: true,
      })))).toMatchObject({ loading: false, ready: true, error: null });
    },
  );

  it('clears an actual first-load error when retry begins', () => {
    const first = beginActivityOverviewRead(
      emptyActivityOverviewReadState(),
      'root',
      'conversation:root',
      false,
    );
    const failed = settleActivityOverviewRead(first.state, first.token, {
      applied: false,
      hasData: false,
      error: 'network failed',
    });
    const requirements = [{
      section: 'root' as const,
      scopeKey: 'conversation:root',
      hasData: false,
    }];
    expect(activityOverviewReadPresentation(failed, requirements)).toMatchObject({
      loading: false,
      error: 'network failed',
    });
    const retry = beginActivityOverviewRead(
      failed,
      'root',
      'conversation:root',
      false,
    );
    expect(activityOverviewReadPresentation(retry.state, requirements)).toMatchObject({
      loading: true,
      error: null,
    });
  });
});
