import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import DeliverableAudio from './DeliverableAudio.svelte';
import AudioPlaylist from './AudioPlaylist.svelte';
import { audioPlayer } from '$lib/stores/audio-player';
import type { AudioJob, SpeechPart } from '$lib/types/api';

const client = { context: vi.fn(), estimates: vi.fn(), create: vi.fn(), cancel: vi.fn(), get: vi.fn() };
const estimates = [
  { length: 'shorter', target_words: 180, seconds_min: 60, seconds_max: 120 },
  { length: 'default', target_words: 300, seconds_min: 120, seconds_max: 180 },
  { length: 'longer', target_words: 450, seconds_min: 180, seconds_max: 240 }
];
const context = (jobs: AudioJob[] = []) => ({
  supported: true, can_generate: true, can_cancel: true, detail: null, jobs
});
const source = 'dlv_11111111111111111111111111111111';
const job = (status: AudioJob['status'] = 'queued'): AudioJob => ({
  job_id: 'audio-recording', deliverable_id: source, mode: 'podcast', status,
  completed_parts: 0, total_parts: 0, parts: [], script_artifact_id: null,
  script_url: null, error_code: null, created_at: '2026-01-01T00:00:00Z'
});
async function open() {
  await fireEvent.click(screen.getByRole('button', { name: 'Listen to this document' }));
  await waitFor(() => expect(client.context).toHaveBeenCalled());
}
beforeEach(() => {
  client.context.mockResolvedValue(context());
  client.estimates.mockResolvedValue(estimates);
  client.create.mockResolvedValue(job());
  client.cancel.mockResolvedValue(job('cancelled'));
  vi.spyOn(HTMLMediaElement.prototype, 'pause').mockImplementation(() => {});
});
afterEach(() => { cleanup(); audioPlayer.stop(); vi.restoreAllMocks(); vi.clearAllMocks(); });

describe('DeliverableAudio', () => {
  it.each([
    ['generation_internal_error', 'This is a Cognis issue'],
    ['provider_access_denied', 'API key and model or voice permissions'],
    ['provider_quota_exhausted', 'insufficient quota or credits'],
    ['provider_rate_limited', 'rate-limiting requests'],
    ['provider_request_rejected', 'rejected this request'],
    ['provider_unavailable', 'could not be reached'],
    ['speech_stream_incomplete', 'did not complete its audio stream'],
    ['speech_unavailable', 'No usable speech model or voice']
  ])('shows an actionable %s error without automatically generating again', async (code, detail) => {
    client.context.mockResolvedValue(context([{ ...job('failed'), mode: 'narration', error_code: code }]));
    render(DeliverableAudio, { sourceRef: source, client });
    await open();
    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent(detail);
    expect(alert).toHaveTextContent('No automatic retries.');
    expect(client.create).not.toHaveBeenCalled();
  });

  it('pins background generation to the displayed revision and restores queued work', async () => {
    render(DeliverableAudio, { sourceRef: source, client });
    await open();
    await fireEvent.click(screen.getByRole('tab', { name: 'Podcast' }));
    await screen.findByText('~3–4 min');
    expect(screen.getByRole('radio', { name: /Default/ })).toBeChecked();
    await fireEvent.click(screen.getByRole('radio', { name: /Longer/ }));
    client.context.mockResolvedValue(context([job()]));
    await fireEvent.click(screen.getByRole('button', { name: 'Generate podcast' }));
    await waitFor(() => expect(client.create).toHaveBeenCalledWith(expect.objectContaining({
      content_ref: source, mode: 'podcast', length: 'longer', idempotency_key: expect.any(String)
    })));
    await screen.findByRole('button', { name: 'Cancel generation' });
    await fireEvent.click(screen.getByRole('button', { name: 'Cancel generation' }));
    expect(client.cancel).toHaveBeenCalledWith('audio-recording');
  });

  it('reuses the same idempotency key after uncertain submission', async () => {
    client.create.mockRejectedValue(new Error('Connection lost'));
    render(DeliverableAudio, { sourceRef: source, client });
    await open();
    await fireEvent.click(screen.getByRole('tab', { name: 'Podcast' }));
    await waitFor(() => expect(screen.getByRole('button', { name: 'Generate podcast' })).toBeEnabled());
    const button = screen.getByRole('button', { name: 'Generate podcast' });
    await fireEvent.click(button);
    await screen.findByText('Connection lost');
    await fireEvent.click(button);
    await waitFor(() => expect(client.create).toHaveBeenCalledTimes(2));
    expect(client.create.mock.calls[0][0].idempotency_key)
      .toBe(client.create.mock.calls[1][0].idempotency_key);
  });

  it('does not keep the prior revision recording selected', async () => {
    client.context.mockResolvedValue(context([job()]));
    const view = render(DeliverableAudio, { sourceRef: source, client });
    await open();
    await screen.findByRole('button', { name: 'Cancel generation' });
    client.context.mockResolvedValue(context());
    await view.rerender({ sourceRef: 'dlv_22222222222222222222222222222222', client });
    await waitFor(() => expect(screen.queryByRole('button', { name: 'Cancel generation' })).toBeNull());
  });

  it('uses the same controls for artifacts without a settings shortcut', async () => {
    const ref = 'art_33333333333333333333333333333333';
    render(DeliverableAudio, { sourceRef: ref, client });
    await open();
    await fireEvent.click(screen.getByRole('button', { name: 'Generate narration' }));
    expect(client.create).toHaveBeenCalledWith(expect.objectContaining({ content_ref: ref }));
    expect(screen.queryByRole('link', { name: 'Configure narrative and speech models' })).toBeNull();
  });

  it('shows all estimates before generation and fetches them once for this revision', async () => {
    render(DeliverableAudio, { sourceRef: source, client });
    await open();
    expect(client.estimates).not.toHaveBeenCalled();
    await fireEvent.click(screen.getByRole('tab', { name: 'Podcast' }));
    await screen.findByText('~1–2 min');
    expect(screen.getByText('~2–3 min')).toBeVisible();
    expect(screen.getByText('~3–4 min')).toBeVisible();
    expect(client.create).not.toHaveBeenCalled();
    expect(client.estimates).toHaveBeenCalledWith(source, expect.objectContaining({ signal: expect.any(AbortSignal) }));
    await fireEvent.click(screen.getByRole('button', { name: 'Close audio controls' }));
    await open();
    expect(client.estimates).toHaveBeenCalledTimes(1);
  });

  it('keeps cancellation available when estimates fail', async () => {
    client.context.mockResolvedValue(context([job()]));
    client.estimates.mockRejectedValue(new Error('Storage unavailable'));
    render(DeliverableAudio, { sourceRef: source, client });
    await open();
    await screen.findByText('Length estimates are unavailable. Try again.');
    expect(screen.getByRole('button', { name: 'Generate podcast' })).toBeDisabled();
    await fireEvent.click(screen.getByRole('button', { name: 'Cancel generation' }));
    expect(client.cancel).toHaveBeenCalledWith('audio-recording');
  });

  it('can show read-only estimates without generation authority or a working TTS route', async () => {
    client.context.mockResolvedValue({
      ...context(), can_generate: false, can_cancel: false, can_estimate: true
    });
    render(DeliverableAudio, { sourceRef: source, client });
    await open();
    await fireEvent.click(screen.getByRole('tab', { name: 'Podcast' }));
    await screen.findByText('~2–3 min');
    expect(screen.getByRole('button', { name: 'Generate podcast' })).toBeDisabled();
    expect(client.create).not.toHaveBeenCalled();
  });

  it('fences late estimates from the previous source revision', async () => {
    let finish!: (value: typeof estimates) => void;
    let finishCurrent!: (value: typeof estimates) => void;
    client.estimates.mockImplementationOnce(() => new Promise(resolve => { finish = resolve; }));
    client.estimates.mockImplementationOnce(() => new Promise(resolve => { finishCurrent = resolve; }));
    const view = render(DeliverableAudio, { sourceRef: source, client });
    await open();
    await fireEvent.click(screen.getByRole('tab', { name: 'Podcast' }));
    await waitFor(() => expect(client.estimates).toHaveBeenCalledTimes(1));
    await view.rerender({ sourceRef: 'dlv_22222222222222222222222222222222', client });
    finish(estimates.map(plan => ({ ...plan, seconds_min: 540, seconds_max: 600 })));
    await fireEvent.click(screen.getByRole('tab', { name: 'Podcast' }));
    await waitFor(() => expect(client.estimates).toHaveBeenCalledTimes(2));
    expect(screen.queryByText('~9–10 min')).toBeNull();
    finishCurrent(estimates);
    await screen.findByText('~2–3 min');
    expect(client.estimates.mock.calls[1][0]).toBe('dlv_22222222222222222222222222222222');
  });

  it('disables paid work for read-only and unsupported sources', async () => {
    client.context.mockResolvedValue({ ...context(), supported: false, can_generate: false,
      detail: 'No readable text.' });
    render(DeliverableAudio, { sourceRef: source, client });
    await fireEvent.click(screen.getByRole('button', { name: 'Listen to this document' }));
    await screen.findByText('No readable text.');
    expect(screen.getByRole('button', { name: 'Generate narration' })).toBeDisabled();
    expect(client.create).not.toHaveBeenCalled();
  });

  it('can cancel owned paid work after speech configuration becomes unavailable', async () => {
    client.context.mockResolvedValue({
      ...context([job()]), can_generate: false, detail: 'Speech is disabled.'
    });
    render(DeliverableAudio, { sourceRef: source, client });
    await open();
    await screen.findByText('Speech is disabled.');
    expect(screen.getByRole('button', { name: 'Generate podcast' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Cancel generation' })).toBeEnabled();
    await fireEvent.click(screen.getByRole('button', { name: 'Cancel generation' }));
    expect(client.cancel).toHaveBeenCalledWith('audio-recording');
    expect(client.create).not.toHaveBeenCalled();
  });

  it('closes the portal without destroying an existing player', async () => {
    client.context.mockResolvedValue(context([{ ...job('completed'), parts: [{
      artifact_id: 'art_recording', audio_url: '/recording', content_type: 'audio/mpeg'
    }] }]));
    render(DeliverableAudio, { sourceRef: source, client });
    await open();
    const audio = await screen.findByLabelText('Audio part 1 of 1');
    await fireEvent.click(screen.getByRole('button', { name: 'Listen to this document' }));
    expect(audio.isConnected).toBe(true);
    expect(screen.getByTestId('deliverable-audio')).not.toBeVisible();
  });

  it('selects the latest recording in each format without a version dropdown or script link', async () => {
    const recording = (id: string, mode: AudioJob['mode'], created_at: string): AudioJob => ({
      ...job('completed'), job_id: id, mode, created_at, script_url: '/transcript.txt',
      parts: [{ artifact_id: `art_${id}`, audio_url: `/${id}.mp3`, content_type: 'audio/mpeg' }]
    });
    client.context.mockResolvedValue(context([
      recording('older-reading', 'narration', '2026-01-01T00:00:00Z'),
      recording('latest-reading', 'narration', '2026-01-03T00:00:00Z'),
      recording('latest-podcast', 'podcast', '2026-01-02T00:00:00Z')
    ]));
    render(DeliverableAudio, { sourceRef: source, client });
    await open();
    await waitFor(() => expect(screen.getByLabelText('Audio part 1 of 1')).toHaveAttribute('src', '/latest-reading.mp3'));
    expect(screen.queryByLabelText('Recording version')).toBeNull();
    expect(screen.queryByRole('link', { name: /script/i })).toBeNull();
    expect(screen.getByRole('button', { name: 'Back 10 seconds' })).toBeVisible();
    expect(screen.getByRole('button', { name: /^Playback speed: / })).toBeVisible();
    await fireEvent.click(screen.getByRole('tab', { name: 'Podcast' }));
    await waitFor(() => expect(screen.getByLabelText('Audio part 1 of 1')).toHaveAttribute('src', '/latest-podcast.mp3'));
    await fireEvent.keyDown(screen.getByRole('tab', { name: 'Podcast' }), { key: 'ArrowLeft' });
    expect(screen.getByRole('tab', { name: 'Reading' })).toHaveAttribute('aria-selected', 'true');
    expect(screen.getByRole('tab', { name: 'Reading' })).toHaveFocus();
    expect(client.create).not.toHaveBeenCalled();
  });

  it('retains the latest usable recording when a later generation fails', async () => {
    client.context.mockResolvedValue(context([
      { ...job('failed'), job_id: 'latest-attempt', created_at: '2026-01-02T00:00:00Z',
        parts: [{ artifact_id: 'art_partial', audio_url: '/partial.mp3', content_type: 'audio/mpeg' }] },
      { ...job('completed'), parts: [{ artifact_id: 'art_saved', audio_url: '/saved.mp3', content_type: 'audio/mpeg' }] }
    ]));
    render(DeliverableAudio, { sourceRef: source, client });
    await open();
    await screen.findByRole('alert');
    expect(await screen.findByLabelText('Audio part 1 of 1')).toHaveAttribute('src', '/saved.mp3');
    expect(client.create).not.toHaveBeenCalled();
  });

  it('plays a committed prefix without making the viewer own the unfinished job', async () => {
    const generating: AudioJob = { ...job('synthesizing'), completed_parts: 1, total_parts: 2,
      parts: [{ artifact_id: 'art_committed', audio_url: '/first.mp3', content_type: 'audio/mpeg' }] };
    client.context.mockResolvedValue(context([generating]));
    client.get.mockResolvedValue(generating);
    const view = render(DeliverableAudio, { sourceRef: source, client });
    await open();
    await fireEvent.click(screen.getByRole('tab', { name: 'Podcast' }));
    expect(await screen.findByLabelText('Audio part 1 of 1')).toHaveAttribute('src', '/first.mp3');
    expect(screen.getByRole('button', { name: 'Cancel generation' })).toBeEnabled();
    view.unmount();
    expect(client.cancel).not.toHaveBeenCalled();
  });

  it('uses an icon-only trigger and exposes playing and paused state while collapsed', async () => {
    vi.spyOn(HTMLMediaElement.prototype, 'play').mockResolvedValue();
    client.context.mockResolvedValue(context([{ ...job('completed'), parts: [{
      artifact_id: 'art_saved', audio_url: '/saved.mp3', content_type: 'audio/mpeg'
    }] }]));
    render(DeliverableAudio, { sourceRef: source, client });
    const trigger = screen.getByRole('button', { name: 'Listen to this document' });
    expect(trigger.textContent?.trim()).toBe('');
    await open();
    const audio = await screen.findByLabelText('Audio part 1 of 1');
    await fireEvent.click(screen.getByRole('button', { name: 'Play audio' }));
    await fireEvent.play(audio);
    expect(trigger).toHaveAttribute('data-playback', 'playing');
    await fireEvent.click(trigger);
    expect(trigger).toHaveAttribute('title', 'Document audio · playing');
    await fireEvent.pause(audio);
    expect(trigger).toHaveAttribute('data-playback', 'paused');
    expect(trigger).toHaveAttribute('title', 'Document audio · paused');
  });
});

it('keeps a playing part stable when polling refreshes its signed URL', async () => {
  const part: SpeechPart = { artifact_id: 'art_recording', audio_url: '/audio?token=old', content_type: 'audio/mpeg' };
  const view = render(AudioPlaylist, { parts: [part], playbackKey: 'recording' });
  const audio = await screen.findByLabelText('Audio part 1 of 1');
  await waitFor(() => expect(audio).toHaveAttribute('src', '/audio?token=old'));
  await view.rerender({ parts: [{ ...part, audio_url: '/audio?token=new' }], playbackKey: 'recording' });
  expect(audio).toHaveAttribute('src', '/audio?token=old');
});
