import { cubicOut } from 'svelte/easing';
import { slide, type TransitionConfig } from 'svelte/transition';

export const CHAT_MOTION = {
  activity: 1600,
  message: 280,
  stream: 320,
  disclosure: 180,
  state: 240,
} as const;

export const chatArrivalContext = Symbol('chat-arrival');

/** Scoped to one timeline, never a global history/identity cache. */
export class ChatArrivalRegistry {
  private seen = new Set<string>();

  constructor(ids: Iterable<string>) {
    this.remember(ids);
  }

  remember(ids: Iterable<string>): void {
    for (const id of ids) this.seen.add(id);
  }

  reset(ids: Iterable<string>): void {
    this.seen.clear();
    this.remember(ids);
  }

  claim(id: string, eligible: boolean): boolean {
    const fresh = !this.seen.has(id);
    this.seen.add(id);
    return fresh && eligible;
  }
}

export function reducedChatMotion(): boolean {
  return typeof window === 'undefined'
    || Boolean(window.matchMedia?.('(prefers-reduced-motion: reduce)').matches);
}

/** Only a newly mounted live message can enter; content updates never restart it. */
export function messageArrival(
  node: HTMLElement,
  initial: { id: string; eligible: boolean; registry?: ChatArrivalRegistry },
) {
  let animation: Animation | undefined;
  if (initial.registry?.claim(initial.id, initial.eligible) && !reducedChatMotion()) {
    animation = node.animate?.(
      [{ opacity: 0, transform: 'translateY(8px)' }, { opacity: 1, transform: 'translateY(0)' }],
      { duration: CHAT_MOTION.message, easing: 'cubic-bezier(.16,1,.3,1)' },
    );
  }
  return { destroy: () => animation?.cancel() };
}

/** Retains lazy mounting and Svelte's reversible disclosure lifecycle. */
export function chatDisclosure(node: HTMLElement, options?: { instant?: boolean }): TransitionConfig {
  // No Web Animations support (or a reduced-motion preference) means instant
  // disclosure, not an exception or a CSS-only delayed mount.
  if (options?.instant || reducedChatMotion() || typeof node.animate !== 'function') return { duration: 0 };
  const opacity = Number(getComputedStyle(node).opacity) || 1;
  const config = slide(node, {
    duration: CHAT_MOTION.disclosure,
    easing: cubicOut,
  });
  return {
    ...config,
    css: (t, u) => `${config.css?.(t, u) ?? ''}opacity:${t * opacity};`,
  };
}
