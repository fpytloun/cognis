import '@testing-library/jest-dom/vitest';
import { TestWebLocks } from './web-locks';

Object.defineProperty(navigator, 'locks', { configurable: true, value: new TestWebLocks() });
// jsdom has no layout observer. Geometry tests drive scroll/viewport events explicitly.
globalThis.ResizeObserver = class {
  observe() {}
  unobserve() {}
  disconnect() {}
};

// jsdom has no media queries; Svelte motion reads prefers-reduced-motion at import.
// Tests that need a specific media state still replace this per test.
if (typeof window !== 'undefined' && !window.matchMedia) {
  window.matchMedia = (query: string) => ({
    matches: false, media: query, onchange: null,
    addEventListener() {}, removeEventListener() {}, addListener() {}, removeListener() {}, dispatchEvent: () => false
  });
}

declare global {
  var __sveltekit_dev: { env?: Record<string, string | undefined> } | undefined;
}

globalThis.__sveltekit_dev = {
  ...(globalThis.__sveltekit_dev ?? {}),
  env: globalThis.__sveltekit_dev?.env ?? {}
};
