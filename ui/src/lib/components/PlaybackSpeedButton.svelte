<script lang="ts">
  import { nextPlaybackRate } from '$lib/utils/playback-rate';
  let { rate, onchange }: { rate: number; onchange: (rate: number) => void } = $props();
</script>

<button type="button" class="playback-speed"
  aria-label={`Playback speed: ${rate}×`} title="Playback speed — press to change"
  onclick={() => onchange(nextPlaybackRate(rate))}>{rate}×</button>

<style>
  .playback-speed {
    display: inline-grid; place-items: center; width: 2rem; height: 1.5rem; flex-shrink: 0;
    border: 0; border-radius: 50%; background: transparent; color: inherit;
    font: inherit; font-size: .75rem; font-variant-numeric: tabular-nums; cursor: pointer;
  }
  .playback-speed:focus-visible { outline: 2px solid var(--audio-accent, #93c5fd); outline-offset: 2px; }
</style>
