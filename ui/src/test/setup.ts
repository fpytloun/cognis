import '@testing-library/jest-dom/vitest';

declare global {
  var __sveltekit_dev: { env?: Record<string, string | undefined> } | undefined;
}

globalThis.__sveltekit_dev = {
  ...(globalThis.__sveltekit_dev ?? {}),
  env: globalThis.__sveltekit_dev?.env ?? {}
};
