import { get } from 'svelte/store';
import { describe, expect, it } from 'vitest';

import { emitTabReset, onTabReset, tabResetSignal } from './tabReset';

describe('tabResetSignal', () => {
  it('invokes the handler only for matching hrefs', () => {
    const tasksCalls: number[] = [];
    const agentsCalls: number[] = [];

    const unsubTasks = onTabReset('/tasks', () => {
      tasksCalls.push(get(tabResetSignal)?.nonce ?? -1);
    });
    const unsubAgents = onTabReset('/agents', () => {
      agentsCalls.push(get(tabResetSignal)?.nonce ?? -1);
    });

    emitTabReset('/tasks');
    emitTabReset('/tasks');
    emitTabReset('/agents');

    expect(tasksCalls).toHaveLength(2);
    expect(agentsCalls).toHaveLength(1);

    unsubTasks();
    unsubAgents();

    // Handlers no longer fire after unsubscribe.
    emitTabReset('/tasks');
    emitTabReset('/agents');
    expect(tasksCalls).toHaveLength(2);
    expect(agentsCalls).toHaveLength(1);
  });

  it('produces a distinct nonce per emission', () => {
    const nonces: number[] = [];
    const unsub = onTabReset('/chat', () => {
      nonces.push(get(tabResetSignal)?.nonce ?? -1);
    });

    emitTabReset('/chat');
    emitTabReset('/chat');
    emitTabReset('/chat');

    expect(nonces).toHaveLength(3);
    expect(new Set(nonces).size).toBe(3);

    unsub();
  });
});
