import { readable } from 'svelte/store';

/**
 * A single global ticker for "now" used by relative-time displays.
 *
 * Previously every ChatMessage held its own `setInterval(30s)` to refresh its
 * relative timestamp. With 50+ rendered messages that produced 50+ timers on
 * mobile devices, wasting battery and causing subtle re-renders.
 *
 * With this store there is exactly one timer for the whole app. Components
 * subscribe and Svelte 5 fine-grained reactivity re-renders only the text that
 * actually uses the value.
 */
export const now = readable(Date.now(), (set) => {
  if (typeof window === 'undefined') return;

  let timer: ReturnType<typeof setInterval> | null = null;

  const tick = () => set(Date.now());
  const start = () => {
    stop();
    timer = setInterval(tick, 30_000);
  };
  const stop = () => {
    if (timer !== null) {
      clearInterval(timer);
      timer = null;
    }
  };

  // Pause when the document is hidden to save battery on mobile.
  const onVisibility = () => {
    if (document.hidden) {
      stop();
    } else {
      tick();
      start();
    }
  };

  start();
  document.addEventListener('visibilitychange', onVisibility);

  return () => {
    stop();
    document.removeEventListener('visibilitychange', onVisibility);
  };
});
