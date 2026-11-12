import { describe, expect, it, vi } from 'vitest';
import { conversationTimelineScope, sessionTimelineScope, taskStepTimelineScope } from '$lib/chat-v2/types';
import {
  ConversationUsageController, usageTarget,
  type UsageInput, type UsageResult, type UsageSource, type UsageState,
} from './conversation-usage';

const unavailable: UsageResult = { status: 'unavailable', reason: 'Insufficient lineage' };
function setup() {
  const publish = vi.fn<(state: UsageState) => void>();
  const source = vi.fn<UsageSource>(async () => unavailable);
  const controller = new ConversationUsageController(publish);
  const input: UsageInput = {
    target: usageTarget(conversationTimelineScope('retained-conversation')),
    inclusion: 'direct', active: true, visible: true, source,
  };
  return { publish, source, controller, input };
}

describe('usage target', () => {
  it('uses the conversation identity regardless of its active physical session', () => {
    expect(usageTarget({ ...conversationTimelineScope('retained-conversation'), session_id: 'rotated-session' }))
      .toEqual({ kind: 'conversation', id: 'retained-conversation' });
    expect(usageTarget(conversationTimelineScope('managed-child')))
      .toEqual({ kind: 'conversation', id: 'managed-child' });
  });
  it('pins the explicit session lane without using its parent conversation', () => {
    expect(usageTarget(sessionTimelineScope('delegate-session', 'parent-conversation')))
      .toEqual({ kind: 'execution', id: 'delegate-session' });
    expect(usageTarget({ ...sessionTimelineScope('delegate-session', 'parent-conversation'), session_id: null })).toBeNull();
  });
  it('never broadens unsupported task steps or missing streams', () => {
    expect(usageTarget(taskStepTimelineScope('task', 'step-run', 'parent-conversation'))).toBeNull();
    expect(usageTarget({ ...conversationTimelineScope('retained-conversation'), missing_stream: true })).toBeNull();
  });
});

describe('conversation usage controller', () => {
  it('does no reads while inactive or invisible, including refresh/reconnect demand', async () => {
    const { controller, input, source } = setup();
    controller.update({ ...input, active: false });
    await controller.refresh();
    controller.update({ ...input, visible: false });
    await controller.refresh();
    expect(source).not.toHaveBeenCalled();
    controller.update(input);
    expect(source).toHaveBeenCalledOnce();
    controller.update({ ...input, target: { ...input.target! } });
    expect(source).toHaveBeenCalledOnce();
  });
  it('refreshes on reopen, inclusion and identity changes', async () => {
    const { controller, input, source } = setup();
    controller.update(input);
    controller.update({ ...input, visible: false });
    controller.update(input);
    controller.update({ ...input, inclusion: 'inclusive' });
    controller.update({ ...input, target: { kind: 'execution', id: 'selected-lane' } });
    expect(source).toHaveBeenCalledTimes(4);
    expect(source.mock.calls[2]?.slice(0, 2)).toEqual([input.target, 'inclusive']);
  });
  it('cancels and fences late completion after target changes and teardown', async () => {
    const { controller, input, publish } = setup();
    const pending: ((value: UsageResult) => void)[] = [];
    const signals: AbortSignal[] = [];
    const source = vi.fn<UsageSource>((_target, _inclusion, signal) => {
      signals.push(signal);
      return new Promise<UsageResult>((resolve) => pending.push(resolve));
    });
    controller.update({ ...input, source });
    controller.update({ ...input, source, target: { kind: 'execution', id: 'selected-lane' } });
    expect(signals[0].aborted).toBe(true);
    pending[0](unavailable);
    await Promise.resolve();
    expect(publish).toHaveBeenLastCalledWith({ status: 'loading' });
    controller.destroy();
    expect(signals[1].aborted).toBe(true);
    pending[1](unavailable);
    await Promise.resolve();
    expect(publish).toHaveBeenLastCalledWith({ status: 'loading' });
  });
  it('fences late failures when hidden and retries only the selected identity', async () => {
    const { controller, input, publish } = setup();
    let reject!: (error: Error) => void;
    const source = vi.fn(() => new Promise<UsageResult>((_resolve, fail) => { reject = fail; }));
    controller.update({ ...input, source });
    controller.update({ ...input, source, active: false });
    reject(new Error('Denied'));
    await Promise.resolve();
    expect(publish).toHaveBeenLastCalledWith({ status: 'idle' });
    const denied = vi.fn(async (): Promise<UsageResult> => { throw new Error('Denied'); });
    controller.update({ ...input, source: denied });
    await controller.refresh();
    expect(publish).toHaveBeenLastCalledWith({ status: 'error', message: 'Could not load usage. Try again.' });
    expect(denied).toHaveBeenLastCalledWith(input.target, 'direct', expect.any(AbortSignal));
  });
  it('refreshes the same selection in place and keeps it through a transient failure', async () => {
    const { controller, input, publish } = setup();
    let fail = false;
    const source = vi.fn<UsageSource>(async () => { if (fail) throw new Error('budget'); return unavailable; });
    controller.update({ ...input, source });
    await vi.waitFor(() => expect(publish).toHaveBeenLastCalledWith(unavailable));
    publish.mockClear();
    await controller.refresh();
    // No loader for a refresh of what is already shown.
    expect(publish.mock.calls.map(([state]) => state.status)).toEqual(['unavailable']);
    publish.mockClear();
    fail = true;
    await controller.refresh();
    expect(publish).not.toHaveBeenCalled();
    // A different selection is a new read and does show the loader.
    controller.update({ ...input, source, inclusion: 'inclusive' });
    expect(publish).toHaveBeenCalledWith({ status: 'loading' });
  });
  it('coalesces live invalidations into one read', async () => {
    vi.useFakeTimers();
    try {
      const { controller, input, source } = setup();
      controller.update(input);
      await vi.runAllTimersAsync();
      source.mockClear();
      controller.invalidate(); controller.invalidate(); controller.invalidate();
      await vi.runAllTimersAsync();
      expect(source).toHaveBeenCalledTimes(1);
      controller.invalidate();
      controller.destroy();
      await vi.runAllTimersAsync();
      expect(source).toHaveBeenCalledTimes(1);
    } finally {
      vi.useRealTimers();
    }
  });
  it('keeps unsupported and disconnected identities explicitly unavailable', () => {
    const { controller, input, source, publish } = setup();
    controller.update({ ...input, target: null });
    expect(publish).toHaveBeenLastCalledWith({ status: 'unavailable', reason: 'Usage is unavailable for this selected scope.' });
    controller.update({ ...input, source: null });
    expect(source).not.toHaveBeenCalled();
    expect(publish).toHaveBeenLastCalledWith({ status: 'unavailable', reason: 'Accounting data is not connected yet.' });
  });
});
