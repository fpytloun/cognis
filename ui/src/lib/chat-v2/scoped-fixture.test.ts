import { afterEach, describe, expect, it, vi } from 'vitest';
import { fixtureScopes, ScopedFixtureController } from './scoped-fixture';
import type { ChatRealtimeFrame } from './types';

const originalWebSocket = globalThis.WebSocket;
let controller: ScopedFixtureController | undefined;

afterEach(() => {
  controller?.realtime.disconnect();
  controller = undefined;
  globalThis.WebSocket = originalWebSocket;
});

describe('scoped fixture canonical cursor ownership', () => {
  for (const scope of [fixtureScopes.parent, fixtureScopes.child]) {
    it(`continues ${scope.key} realtime from its actual sync cursor`, async () => {
      controller = new ScopedFixtureController();
      const fixture = controller;
      const frames: ChatRealtimeFrame[] = [];
      const release = fixture.realtime.subscribe(event => {
        if (event.type === 'chat_v2_frame') frames.push(event);
      });
      const snapshot = await fixture.api.snapshot(scope);
      fixture.realtime.acquireChatV2(scope, snapshot.cursor);
      fixture.realtime.reAuthenticate();
      await vi.waitFor(() => expect(fixture.activeSubscriptions).toContain(scope.key));
      const synced = await fixture.api.sync(scope, snapshot.cursor);
      fixture.triggerActiveFrame(scope);
      await vi.waitFor(() => expect(frames).toHaveLength(1));
      expect(frames[0].cursor_before).toBe(synced.cursor_after);
      expect(frames[0].scope?.key).toBe(scope.key);
      fixture.realtime.releaseChatV2(scope.key);
      release();
    });

    it(`retains ${scope.key} history ownership after sync and frame advancement`, async () => {
      controller = new ScopedFixtureController();
      const fixture = controller;
      const snapshot = await fixture.api.snapshot(scope);
      await fixture.api.sync(scope, snapshot.cursor);
      fixture.triggerActiveFrame(scope);
      const other = scope.key === fixtureScopes.parent.key ? fixtureScopes.child : fixtureScopes.parent;
      const otherSnapshot = await fixture.api.snapshot(other);
      await expect(fixture.api.timeline(other, {
        before: snapshot.timeline.before_cursor,
      })).rejects.toThrow('cross-scope or stale backfill cursor');
      const history = await fixture.api.timeline(scope, { before: snapshot.timeline.before_cursor });
      expect(history.items.length).toBeGreaterThan(0);
      expect(history.before_cursor).not.toBe(snapshot.timeline.before_cursor);
      const otherHistory = await fixture.api.timeline(other, {
        before: otherSnapshot.timeline.before_cursor,
      });
      expect(otherHistory.items.length).toBeGreaterThan(0);
    });
  }

  it('forwards authenticated reconnects to the mounted timeline reconciliation owner', async () => {
    controller = new ScopedFixtureController();
    const fixture = controller;
    const types: string[] = [];
    const release = fixture.realtime.subscribe(event => types.push(event.type));
    const snapshot = await fixture.api.snapshot(fixtureScopes.parent);
    fixture.realtime.acquireChatV2(fixtureScopes.parent, snapshot.cursor);
    fixture.realtime.reAuthenticate();
    await vi.waitFor(() => expect(fixture.activeSubscriptions).toContain(fixtureScopes.parent.key));
    fixture.realtime.reAuthenticate();
    await vi.waitFor(() => expect(types.filter(type => type === 'reconnected')).toHaveLength(1));
    fixture.realtime.releaseChatV2(fixtureScopes.parent.key);
    release();
  });
});
