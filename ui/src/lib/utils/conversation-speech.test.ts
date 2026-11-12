import { describe, expect, it, vi } from 'vitest';
import { ConversationSpeech, type SettledVoiceTurn } from './conversation-speech';
import { speechRuntime } from './conversation-speech.test-fixtures';
import type { AssistantDeliverableTimelineItem } from '$lib/chat-v2/types';

function setup() {
  const sentence = vi.fn();
  const complete = vi.fn();
  const idle = vi.fn();
  return { speech: new ConversationSpeech('voice-input', sentence, complete, idle), sentence, complete, idle };
}

describe('Chat v2 conversation speech projection', () => {
  it('reads only owned deliverable revisions once, after canonical turn settlement and before completion', () => {
    const delivered = vi.fn(), complete = vi.fn();
    const speech = new ConversationSpeech('voice-input', vi.fn(), complete, vi.fn(), delivered);
    const item: AssistantDeliverableTimelineItem = {
      id: 'report-reference', kind: 'assistant_deliverable', deliverable_id: 'report-revision',
      turn_id: 'voice-turn', format: 'rich', sort_key: '003', stable: true, source_refs: [],
    };
    speech.update(speechRuntime('', { volatile_items: [item] }));
    expect(delivered).not.toHaveBeenCalled();
    const settled = { turn_id: 'voice-turn', items: [
      { ...item, id: 'old-report', deliverable_id: 'old-report', turn_id: 'previous-turn' },
      { ...item, id: 'unscoped-report', deliverable_id: 'unscoped-report', turn_id: null },
      item, { ...item, id: 'duplicate-reference' },
    ] };
    speech.settle(settled);
    speech.settle(settled);
    expect(delivered.mock.calls).toEqual([['report-revision']]);
    expect(complete).toHaveBeenCalledOnce();
    expect(delivered.mock.invocationCallOrder[0]).toBeLessThan(complete.mock.invocationCallOrder[0]);
  });
  it('speaks cumulative sentences once and flushes a short final answer before completion', () => {
    const { speech, sentence, complete } = setup();
    speech.update(speechRuntime('First sentence. Next'));
    speech.update(speechRuntime('First sentence. Next', { runtime_revision: 2 }));
    speech.update(speechRuntime('First sentence. Next. Yes.', { runtime_revision: 3, has_active_turn: false }));
    speech.settle({ turn_id: 'voice-turn', items: speechRuntime(
      'First sentence. Next. Yes.', { has_active_turn: false }).volatile_items });
    expect(sentence.mock.calls.map(([value]) => value.text)).toEqual(['First sentence.', 'Next. Yes.']);
    expect(sentence.mock.calls.map(([value]) => value.sentence_index)).toEqual([0, 1]);
    expect(complete).toHaveBeenCalledOnce();
    expect(sentence.mock.invocationCallOrder[1]).toBeLessThan(complete.mock.invocationCallOrder[0]);
  });

  it('handles non-streamed responses and does not replay a terminal update', () => {
    const { speech, sentence, complete } = setup();
    const final = speechRuntime('Yes.', { has_active_turn: false });
    speech.update(final);
    speech.update(final);
    speech.settle({ turn_id: 'voice-turn', items: final.volatile_items });
    expect(sentence).toHaveBeenCalledOnce();
    expect(complete).toHaveBeenCalledOnce();
  });

  it('recovers a fast completed answer from canonical items after runtime overlays drain', () => {
    const { speech, sentence, complete } = setup();
    const idle = speechRuntime('', { has_active_turn: false });
    speech.update(idle);
    speech.update(idle, speechRuntime('Already finished.', { has_active_turn: false }).volatile_items);
    speech.settle({ turn_id: 'voice-turn', items: speechRuntime(
      'Already finished.', { has_active_turn: false }).volatile_items });
    expect(sentence).toHaveBeenCalledOnce();
    expect(complete).toHaveBeenCalledOnce();
  });

  it('does not treat message completion between tools as turn completion', () => {
    const { speech, sentence, complete } = setup();
    speech.update(speechRuntime('Checking now.', {}, { status: 'complete', text_streaming: false }));
    speech.update(speechRuntime('Finished.', { runtime_revision: 2 }, {
      id: 'voice-final', message_id: 'voice-final', sort_key: '002', status: 'complete', text_streaming: false,
    }));
    expect(complete).not.toHaveBeenCalled();
    speech.update(speechRuntime('', { runtime_revision: 3, has_active_turn: false }));
    speech.settle({ turn_id: 'voice-turn', items: [] });
    expect(sentence.mock.calls.map(([value]) => value.sentence_index)).toEqual([0, 1]);
    expect(complete).toHaveBeenCalledOnce();
  });

  it('ignores unrelated turns, admission idle frames and stale revisions', () => {
    const { speech, sentence, complete } = setup();
    speech.update(speechRuntime('', { has_active_turn: false, active_turn: null }));
    speech.update(speechRuntime('Another answer.', {
      runtime_revision: 2, has_active_turn: false, active_turn: null,
    }, { turn_id: 'other-turn' }));
    expect(complete).not.toHaveBeenCalled();
    speech.update(speechRuntime('Current answer. ', { runtime_revision: 4 }));
    speech.update(speechRuntime('', { runtime_revision: 3, has_active_turn: false }));
    expect(complete).not.toHaveBeenCalled();
    expect(sentence).toHaveBeenCalledOnce();
  });

  it('excludes split fenced code and strips presentation markup', () => {
    const { speech, sentence } = setup();
    speech.update(speechRuntime('**First sentence.** ```js\nsecret. '));
    speech.update(speechRuntime('**First sentence.** ```js\nsecret. More code.\n``` [Read this](https://cognis.app).', {
      runtime_revision: 2, has_active_turn: false,
    }));
    speech.settle({ turn_id: 'voice-turn', items: speechRuntime(
      '**First sentence.** ```js\nsecret. More code.\n``` [Read this](https://cognis.app).',
      { has_active_turn: false }).volatile_items });
    expect(sentence.mock.calls.map(([value]) => value.text).join(' ')).toBe('First sentence. Read this.');
  });

  it('keeps ordering across multiple boundaries in one cumulative update', () => {
    const { speech, sentence } = setup();
    speech.update(speechRuntime('Sentence one. Sentence two. Sentence three. '));
    expect(sentence.mock.calls.map(([value]) => value.text)).toEqual([
      'Sentence one.', 'Sentence two.', 'Sentence three.',
    ]);
  });
  it('does not permanently close idle recovery before canonical answer arrival', () => {
    const { speech, sentence, complete, idle } = setup();
    speech.update(speechRuntime(''));
    speech.update(speechRuntime('', { runtime_revision: 2, has_active_turn: false }));
    expect(idle).toHaveBeenCalledOnce();
    expect(complete).not.toHaveBeenCalled();
    speech.settle({ turn_id: 'voice-turn', items: speechRuntime(
      'Recovered complete answer.', { has_active_turn: false }).volatile_items });
    expect(sentence).toHaveBeenCalledOnce();
    expect(complete).toHaveBeenCalledOnce();
  });

  it('settles a failed-before-start admission without an observed active turn', () => {
    const { speech, sentence, complete, idle } = setup();
    speech.update(speechRuntime('', { has_active_turn: false }));
    expect(idle).toHaveBeenCalledOnce();
    speech.settle({ turn_id: 'voice-turn', items: [] });
    expect(sentence).not.toHaveBeenCalled();
    expect(complete).toHaveBeenCalledOnce();
  });

  it('retains terminal reconciliation requested during a nonterminal lookup', async () => {
    const { speech, complete } = setup();
    let resolveFirst!: (value: SettledVoiceTurn | null) => void;
    const recover = vi.fn<() => Promise<SettledVoiceTurn | null>>()
      .mockReturnValueOnce(new Promise(resolve => { resolveFirst = resolve; }))
      .mockResolvedValue({ turn_id: 'voice-turn', items: [] });
    const onError = vi.fn();
    const pending = speech.reconcile(recover, () => true, onError);
    await speech.reconcile(recover, () => true, onError);
    expect(recover).toHaveBeenCalledOnce();
    resolveFirst(null);
    await pending;
    expect(recover).toHaveBeenCalledTimes(2);
    expect(complete).toHaveBeenCalledOnce();
    expect(onError).not.toHaveBeenCalled();
  });

  it('does not settle or drain queued recovery after the owning call is invalidated', async () => {
    const { speech, complete } = setup();
    let current = true;
    let resolveFirst!: (value: SettledVoiceTurn | null) => void;
    const recover = vi.fn<() => Promise<SettledVoiceTurn | null>>()
      .mockReturnValueOnce(new Promise(resolve => { resolveFirst = resolve; }));
    const pending = speech.reconcile(recover, () => current, vi.fn());
    await speech.reconcile(recover, () => current, vi.fn());
    current = false;
    resolveFirst({ turn_id: 'voice-turn', items: [] });
    await pending;
    expect(recover).toHaveBeenCalledOnce();
    expect(complete).not.toHaveBeenCalled();
  });
});
