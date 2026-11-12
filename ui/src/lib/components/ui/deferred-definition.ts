import type { Component } from 'svelte';

/** Cache executable definitions only; source data and authority stay with each mount. */
export function deferredDefinition<P extends Record<string, unknown>>(load: () => Promise<{ default: Component<P> }>) {
  let component: Component<P> | null = null;
  let pending: Promise<Component<P>> | null = null;
  return {
    peek: () => component,
    load: () => pending ??= load().then(module => component = module.default).catch(error => {
      pending = null;
      throw error;
    }),
  };
}
export type DeferredDefinition<P extends Record<string, unknown>> = ReturnType<typeof deferredDefinition<P>>;
