import { cleanup, fireEvent, render, screen } from '@testing-library/svelte';
import { afterEach, expect, it, vi } from 'vitest';
import PlaybackSpeedButton from './PlaybackSpeedButton.svelte';
import { nextPlaybackRate, PLAYBACK_RATES } from '$lib/utils/playback-rate';

afterEach(cleanup);

it('shows only the speed and changes it when pressed', async () => {
  const onchange = vi.fn();
  const view = render(PlaybackSpeedButton, { rate: 1, onchange });
  const button = screen.getByRole('button', { name: 'Playback speed: 1×' });
  expect(button).toHaveTextContent('1×');
  await fireEvent.click(button);
  expect(onchange).toHaveBeenCalledWith(1.25);
  await view.rerender({ rate: 1.25, onchange });
  expect(screen.getByRole('button', { name: 'Playback speed: 1.25×' })).toHaveTextContent('1.25×');
});

it('cycles through the existing deliverable speeds and wraps around', () => {
  for (const [index, rate] of PLAYBACK_RATES.entries()) {
    expect(nextPlaybackRate(rate)).toBe(PLAYBACK_RATES[(index + 1) % PLAYBACK_RATES.length]);
  }
});
