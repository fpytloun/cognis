<script lang="ts">
  import { onDestroy, untrack } from 'svelte';
  import { Play, Pause, Volume2, RotateCcw, RotateCw, Download } from 'lucide-svelte';
  import type { SpeechPart } from '$lib/types/api';
  import { audioPlayer } from '$lib/stores/audio-player';
  import PlaybackSpeedButton from '$lib/components/PlaybackSpeedButton.svelte';
  import { applyMediaPlaybackRate } from '$lib/utils/playback-rate';
  let { parts, playbackKey, rich = false, complete = true }: {
    parts: SpeechPart[]; playbackKey: string; rich?: boolean; complete?: boolean;
  } = $props();
  let audio: HTMLAudioElement;
  let index = $state(0), time = $state(0), duration = $state(0);
  let playing = $state(false), error = $state(''), src = $state('');
  let started = $state(false);
  let waiting = $state(false);
  let restoreTime = 0, playAfterLoad = false, playingPart = '';
  const storageKey = untrack(() => `cognis.audio-position.${playbackKey}`);
  try {
    const position = JSON.parse(localStorage.getItem(storageKey) ?? 'null');
    if (position && Number.isInteger(position.part) && position.part >= 0 && position.part < untrack(() => parts.length)) {
      index = position.part;
      restoreTime = Math.max(0, Number(position.time) || 0);
    }
  } catch { /* Browser-local resume is optional. */ }
  $effect(() => {
    const part = parts[index];
    const position = `${index}:${part?.artifact_id ?? ''}`;
    // Refreshing signed links must not reset audio that is already playing.
    if (part && (position !== playingPart || (error && part.audio_url !== src))) {
      const reload = src === part.audio_url;
      playingPart = position; src = part.audio_url; error = '';
      if (reload && audio) audio.load();
    }
  });
  function remember() {
    time = audio?.currentTime ?? 0;
    try { localStorage.setItem(storageKey, JSON.stringify({ part: index, time })); } catch { /* Optional. */ }
  }
  $effect(() => {
    if (audio) applyMediaPlaybackRate(audio, $audioPlayer.playbackRate);
  });
  $effect(() => {
    const ownsPlayback = $audioPlayer.currentKey === playbackKey;
    if (!waiting) return;
    if (!ownsPlayback) { waiting = false; return; }
    if (index + 1 < parts.length) {
      waiting = false; restoreTime = 0; playAfterLoad = true; index += 1;
    } else if (complete) {
      waiting = false; started = false; audioPlayer.stopIfKey(playbackKey, audio);
    }
  });
  async function play() {
    // Explicit playback owns pending loads too, including a completed playlist's restart.
    audioPlayer.attach(playbackKey, audio, { togglePause: toggle });
    if (audio.ended) {
      if (!complete && index + 1 === parts.length) {
        waiting = true; audioPlayer.buffering(playbackKey, audio, true); return;
      }
      if (index > 0) { restoreTime = 0; playAfterLoad = true; index = 0; return; }
      audio.currentTime = 0;
    }
    try { await audio.play(); } catch {
      if (audioPlayer.isCurrent(playbackKey, audio)) {
        audioPlayer.stopIfKey(playbackKey, audio);
        error = 'Playback could not start. Try Play again.';
      }
    }
  }
  function seek(seconds: number) {
    if (!audio || !Number.isFinite(audio.duration)) return;
    const target = audio.currentTime + seconds;
    if (target < 0 && index > 0) {
      restoreTime = target; playAfterLoad = playing; index -= 1;
    } else if (target > audio.duration && index + 1 < parts.length) {
      restoreTime = target - audio.duration; playAfterLoad = playing; index += 1;
    } else {
      audio.currentTime = Math.max(0, Math.min(target, audio.duration)); remember();
    }
  }
  async function toggle() {
    if (playing || waiting) {
      waiting = false; audio.pause(); audioPlayer.buffering(playbackKey, audio, false);
    } else await play();
  }
  function loaded() {
    duration = Number.isFinite(audio.duration) ? audio.duration : 0;
    const target = restoreTime < 0 ? duration + restoreTime : restoreTime;
    if (target < 0 && index > 0) { restoreTime = target; index -= 1; return; }
    if (target > duration && index + 1 < parts.length) {
      restoreTime = target - duration; index += 1; return;
    }
    audio.currentTime = Math.max(0, Math.min(target, Math.max(0, duration - 0.05)));
    restoreTime = 0; remember();
    if (playAfterLoad) {
      playAfterLoad = false;
      if (audioPlayer.isCurrent(playbackKey, audio)) void play();
    }
  }
  function ended() {
    if (!audioPlayer.isCurrent(playbackKey, audio)) return;
    if (index + 1 < parts.length) {
      restoreTime = 0; playAfterLoad = true; index += 1;
    } else if (!complete) {
      playing = false; waiting = true; audioPlayer.buffering(playbackKey, audio, true);
    } else { playing = false; started = false; audioPlayer.stopIfKey(playbackKey, audio); }
  }
  function format(value: number) {
    return `${Math.floor(value / 60)}:${Math.floor(value % 60).toString().padStart(2, '0')}`;
  }
  onDestroy(() => { remember(); if (audio) audioPlayer.stopIfKey(playbackKey, audio); });
</script>

<div class="space-y-2" aria-label="Audio playback">
  <!-- Native media plumbing without the full browser player chrome. -->
  <!-- svelte-ignore a11y_media_has_caption -->
  <audio bind:this={audio} {src} aria-label={`Audio part ${index + 1} of ${parts.length}`}
    preload="metadata" onloadedmetadata={loaded} ontimeupdate={remember}
    onended={ended} onpause={() => { playing = false; }}
    onplay={() => {
      if (!audioPlayer.isCurrent(playbackKey, audio)) { audio.pause(); return; }
      playing = true; started = true; applyMediaPlaybackRate(audio, $audioPlayer.playbackRate);
    }}
    onerror={() => { playing = false; audioPlayer.stopIfKey(playbackKey, audio); error = 'Audio could not load. Reopen Listen to refresh the link.'; }}></audio>
  <div class="audio-playlist-controls flex items-center gap-2">
    <div class="audio-transport" role="group" aria-label="Audio transport">
    {#if rich || playing}
    <button type="button" aria-label="Back 10 seconds" title="Back 10 seconds" onclick={() => seek(-10)}
      class="relative inline-flex h-11 w-11 items-center justify-center rounded-full hover:bg-surface-hover">
      <RotateCcw class="h-7 w-7" /><span aria-hidden="true" class="absolute text-[9px] font-semibold">10</span>
    </button>
    {/if}
    <button type="button" aria-label={playing || waiting ? 'Pause audio' : 'Play audio'} title={playing || waiting ? 'Pause' : 'Play'}
      onclick={() => void toggle()}
      class="inline-flex h-11 w-11 items-center justify-center rounded-full bg-accent text-white">
      {#if playing}<Pause class="h-5 w-5" />{:else if rich || started}<Play class="h-5 w-5" />{:else}<Volume2 class="h-5 w-5" />{/if}
    </button>
    {#if rich || playing}
    <button type="button" aria-label="Forward 10 seconds" title="Forward 10 seconds" onclick={() => seek(10)}
      class="relative inline-flex h-11 w-11 items-center justify-center rounded-full hover:bg-surface-hover">
      <RotateCw class="h-7 w-7" /><span aria-hidden="true" class="absolute text-[9px] font-semibold">10</span>
    </button>
    {/if}
    </div>
    {#if rich || playing}
      <PlaybackSpeedButton rate={$audioPlayer.playbackRate} onchange={audioPlayer.setPlaybackRate} />
    {/if}
    {#if rich || playing}<span class="audio-time">{format(time)} / {format(duration)}</span>{/if}
    {#if rich}
      <a href={parts[index]?.audio_url} download aria-label="Download this audio part" title="Download this audio part"><Download /></a>
    {/if}
  </div>
  {#if rich}
      <input type="range" class="audio-position" min="0" max={Math.max(0, duration)} step="1"
        aria-label="Audio position" value={time}
        oninput={(event) => { audio.currentTime = Number(event.currentTarget.value); remember(); }} />
      {#if parts.length > 1}<span class="audio-part">Part {index + 1} of {parts.length}</span>{/if}
  {/if}
  {#if error}<p role="alert" class="text-xs text-danger">{error}</p>{/if}
  {#if waiting}<p role="status" class="text-xs text-muted">Waiting for the next audio part…</p>{/if}
</div>

<style>
  .audio-playlist-controls { display: flex; align-items: center; flex-wrap: wrap; gap: .25rem; }
  .audio-transport { display: inline-flex; align-items: center; gap: 0; }
  .audio-playlist-controls button {
    position: relative; display: inline-grid; place-items: center; width: 2.75rem;
    height: 2.75rem; border: 0; border-radius: 50%; background: transparent; color: inherit; cursor: pointer;
  }
  .audio-playlist-controls button > span { position: absolute; font-size: 9px; }
  .audio-playlist-controls :global(svg) { width: 1.5rem; height: 1.5rem; }
  .audio-playlist-controls button:hover, .audio-playlist-controls a:hover { background: color-mix(in srgb, currentColor 10%, transparent); }
  .audio-playlist-controls a { display: inline-grid; place-items: center; width: 1.75rem; height: 2.75rem; color: inherit; }
  .audio-playlist-controls a :global(svg) { width: 1rem; height: 1rem; }
  .audio-time, .audio-part { font-size: .7rem; font-variant-numeric: tabular-nums; color: var(--audio-muted, inherit); white-space: nowrap; }
  .audio-time { margin-left: auto; }
  .audio-position { display: block; width: 100%; margin: .25rem 0; accent-color: var(--audio-accent, #93c5fd); }
  .audio-playlist-controls :is(button, a):focus-visible { outline: 2px solid var(--audio-accent, #93c5fd); outline-offset: 2px; }
  @media (pointer: coarse) {
    .audio-transport button { min-width: 44px; min-height: 44px; }
    .audio-playlist-controls a { min-width: 2.75rem; }
  }
</style>
