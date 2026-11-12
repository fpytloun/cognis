import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import { get } from 'svelte/store';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { audioPlayer } from '$lib/stores/audio-player';
import AudioPlaylist from './AudioPlaylist.svelte';

const parts = [
  { artifact_id: 'first-part', audio_url: '/first.mp3', content_type: 'audio/mpeg' },
  { artifact_id: 'second-part', audio_url: '/second.mp3', content_type: 'audio/mpeg' }
];
afterEach(() => { cleanup(); audioPlayer.stop(); audioPlayer.setPlaybackRate(1); vi.restoreAllMocks(); });
beforeEach(() => {
  localStorage.clear();
  vi.spyOn(HTMLMediaElement.prototype, 'play').mockImplementation(function(this: HTMLMediaElement) {
    this.dispatchEvent(new Event('play')); return Promise.resolve();
  });
  vi.spyOn(HTMLMediaElement.prototype, 'pause').mockImplementation(function(this: HTMLMediaElement) {
    this.dispatchEvent(new Event('pause'));
  });
});
async function load(audio: HTMLAudioElement, duration: number) {
  Object.defineProperty(audio, 'duration', { configurable: true, value: duration });
  await fireEvent.loadedMetadata(audio);
}
describe('compact audio controls', () => {
  it('plays the committed prefix and resumes when the next part appears', async () => {
    const view = render(AudioPlaylist, { parts: [parts[0]], playbackKey: 'growing-audio', complete: false });
    const audio = view.container.querySelector('audio')!;
    await load(audio, 30);
    await fireEvent.click(screen.getByRole('button', { name: 'Play audio' }));
    Object.defineProperty(audio, 'ended', { configurable: true, value: true });
    await fireEvent.ended(audio);
    expect(screen.getByRole('status')).toHaveTextContent('Waiting for the next audio part');
    expect(get(audioPlayer).currentKey).toBe('growing-audio');
    await view.rerender({ parts, playbackKey: 'growing-audio', complete: false });
    await waitFor(() => expect(audio.getAttribute('src')).toBe('/second.mp3'));
    Object.defineProperty(audio, 'ended', { configurable: true, value: false });
    await load(audio, 40);
    expect(HTMLMediaElement.prototype.play).toHaveBeenCalledTimes(2);
  });

  it('does not reclaim playback when a newer player took over during generation', async () => {
    const view = render(AudioPlaylist, { parts: [parts[0]], playbackKey: 'growing-audio', complete: false });
    const audio = view.container.querySelector('audio')!;
    await load(audio, 30);
    await fireEvent.click(screen.getByRole('button', { name: 'Play audio' }));
    await fireEvent.ended(audio);
    audioPlayer.attach('other-recording', document.createElement('audio'));
    await view.rerender({ parts, playbackKey: 'growing-audio', complete: false });
    expect(get(audioPlayer).currentKey).toBe('other-recording');
    expect(HTMLMediaElement.prototype.play).toHaveBeenCalledOnce();
  });
  it('releases its ownership when the browser rejects playback', async () => {
    vi.mocked(HTMLMediaElement.prototype.play).mockRejectedValueOnce(new DOMException('Playback denied', 'NotAllowedError'));
    const view = render(AudioPlaylist, { parts, playbackKey: 'denied-playback' });
    await load(view.container.querySelector('audio')!, 30);
    await fireEvent.click(screen.getByRole('button', { name: 'Play audio' }));
    await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent('Playback could not start.'));
    expect(get(audioPlayer).currentKey).toBeNull();
  });
  it('does not release or report failure for a newer same-key owner after a stale play rejection', async () => {
    let rejectPlay!: (reason: Error) => void;
    vi.mocked(HTMLMediaElement.prototype.play).mockReturnValueOnce(new Promise<void>((_, reject) => { rejectPlay = reject; }));
    const view = render(AudioPlaylist, { parts, playbackKey: 'pending-playback' });
    try {
      await load(view.container.querySelector('audio')!, 30);
      await fireEvent.click(screen.getByRole('button', { name: 'Play audio' }));
      const newerAudio = document.createElement('audio');
      audioPlayer.attach('pending-playback', newerAudio);
      await act(() => rejectPlay(new DOMException('Playback interrupted', 'AbortError')));
      expect(audioPlayer.isCurrent('pending-playback', newerAudio)).toBe(true);
      expect(screen.queryByRole('alert')).toBeNull();
    } finally {
      audioPlayer.stop();
    }
  });
  it.each(['newer-playback', 'delayed-playlist'])('does not reclaim a delayed part from another element under key %s', async (newerKey) => {
    const view = render(AudioPlaylist, { parts, playbackKey: 'delayed-playlist' });
    const audio = view.container.querySelector('audio')!;
    try {
      await load(audio, 30);
      await fireEvent.click(screen.getByRole('button', { name: 'Play audio' }));
      await fireEvent.ended(audio);
      await waitFor(() => expect(audio.getAttribute('src')).toBe('/second.mp3'));

      const newerAudio = document.createElement('audio');
      audioPlayer.attach(newerKey, newerAudio);
      await newerAudio.play();
      const playCount = vi.mocked(HTMLMediaElement.prototype.play).mock.calls.length;
      await load(audio, 40);

      expect(get(audioPlayer).currentKey).toBe(newerKey);
      expect(HTMLMediaElement.prototype.play).toHaveBeenCalledTimes(playCount);
    } finally {
      audioPlayer.stop();
    }
  });
  it('restarts a completed multipart recording after loading its first part', async () => {
    const view = render(AudioPlaylist, { parts, playbackKey: 'restart-playlist' });
    const audio = view.container.querySelector('audio')!;
    await load(audio, 30);
    await fireEvent.click(screen.getByRole('button', { name: 'Play audio' }));
    await fireEvent.ended(audio);
    await waitFor(() => expect(audio.getAttribute('src')).toBe('/second.mp3'));
    await load(audio, 40);
    Object.defineProperty(audio, 'ended', { configurable: true, value: true });
    await fireEvent.ended(audio);
    expect(get(audioPlayer).currentKey).toBeNull();

    await fireEvent.click(screen.getByRole('button', { name: 'Play audio' }));
    await waitFor(() => expect(audio.getAttribute('src')).toBe('/first.mp3'));
    Object.defineProperty(audio, 'ended', { configurable: true, value: false });
    await load(audio, 30);
    expect(HTMLMediaElement.prototype.play).toHaveBeenCalledTimes(3);
    expect(get(audioPlayer).currentKey).toBe('restart-playlist');
  });
  it('does not adopt a stale play or ended event after another element takes ownership', async () => {
    const view = render(AudioPlaylist, { parts, playbackKey: 'stale-events' });
    const audio = view.container.querySelector('audio')!;
    try {
      await load(audio, 30);
      await fireEvent.click(screen.getByRole('button', { name: 'Play audio' }));
      const newerAudio = document.createElement('audio');
      audioPlayer.attach('stale-events', newerAudio);
      await fireEvent.play(audio);
      await fireEvent.ended(audio);
      expect(get(audioPlayer).currentKey).toBe('stale-events');
      expect(audio.getAttribute('src')).toBe('/first.mp3');
      expect(audioPlayer.isCurrent('stale-events', newerAudio)).toBe(true);
    } finally {
      audioPlayer.stop();
    }
  });
  it('reloads a repeated cached artifact when advancing playlist position', async () => {
    const reload = vi.spyOn(HTMLMediaElement.prototype, 'load').mockImplementation(() => {});
    const view = render(AudioPlaylist, { parts: [parts[0], parts[0]], playbackKey: 'repeated-parts' });
    const audio = view.container.querySelector('audio')!;
    await load(audio, 30);
    await fireEvent.click(screen.getByRole('button', { name: 'Play audio' }));
    await fireEvent.ended(audio);
    await waitFor(() => expect(reload).toHaveBeenCalledOnce());
    await load(audio, 30);
    expect(HTMLMediaElement.prototype.play).toHaveBeenCalledTimes(2);
  });
  it('offers play/pause and circular 10-second seeking without full player chrome', async () => {
    const view = render(AudioPlaylist, { parts, playbackKey: 'compact-audio' });
    const audio = view.container.querySelector('audio')!;
    await load(audio, 60);
    expect(screen.queryByRole('button', { name: 'Back 10 seconds' })).toBeNull();
    await fireEvent.click(screen.getByRole('button', { name: 'Play audio' }));
    audio.currentTime = 25;
    await fireEvent.timeUpdate(audio);
    await fireEvent.click(screen.getByRole('button', { name: 'Back 10 seconds' }));
    expect(audio.currentTime).toBe(15);
    await fireEvent.click(screen.getByRole('button', { name: 'Forward 10 seconds' }));
    expect(audio.currentTime).toBe(25);
    await waitFor(() => expect(screen.getByRole('button', { name: 'Pause audio' })).toBeTruthy());
    await fireEvent.click(screen.getByRole('button', { name: 'Pause audio' }));
    expect(screen.queryByRole('button', { name: 'Back 10 seconds' })).toBeNull();
    expect(screen.queryByText(/\d+:\d+ \/ \d+:\d+/)).toBeNull();
    expect(screen.queryByText(/^Part \d+\/\d+$/)).toBeNull();
    expect(audio.controls).toBe(false);
    expect(screen.queryByRole('slider')).toBeNull();
    expect(screen.queryByRole('combobox')).toBeNull();
  });
  it('seeks forward and backward across part boundaries', async () => {
    const view = render(AudioPlaylist, { parts, playbackKey: 'cross-part-seek' });
    const audio = view.container.querySelector('audio')!;
    await load(audio, 30);
    await fireEvent.click(screen.getByRole('button', { name: 'Play audio' }));
    audio.currentTime = 25;
    await fireEvent.click(screen.getByRole('button', { name: 'Forward 10 seconds' }));
    await waitFor(() => expect(audio.getAttribute('src')).toBe('/second.mp3'));
    await load(audio, 40);
    expect(audio.currentTime).toBe(5);
    await fireEvent.click(screen.getByRole('button', { name: 'Back 10 seconds' }));
    await waitFor(() => expect(audio.getAttribute('src')).toBe('/first.mp3'));
    await load(audio, 30);
    expect(audio.currentTime).toBe(25);
  });
  it('shows full document controls immediately, without another collapsed speaker button', async () => {
    render(AudioPlaylist, { parts, playbackKey: 'rich-audio', rich: true });
    expect(screen.getByRole('slider', { name: 'Audio position' })).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Playback speed: 1×' })).toHaveTextContent('1×');
    expect(screen.getByRole('button', { name: 'Back 10 seconds' })).toBeVisible();
    expect(screen.getByRole('button', { name: 'Forward 10 seconds' })).toBeVisible();
    await fireEvent.click(screen.getByRole('button', { name: 'Playback speed: 1×' }));
    await fireEvent.click(screen.getByRole('button', { name: 'Playback speed: 1.25×' }));
    expect(screen.getByLabelText('Audio part 1 of 2')).toHaveProperty('playbackRate', 1.5);
  });
});
