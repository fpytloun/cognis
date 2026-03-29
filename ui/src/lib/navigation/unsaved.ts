import type { BeforeNavigate } from '@sveltejs/kit';

export interface UnsavedChangesOptions {
  message?: string;
}

const DEFAULT_MESSAGE = 'You have unsaved changes. Leave this page?';

export function installBeforeUnloadGuard(
  isDirty: () => boolean,
  options: UnsavedChangesOptions = {}
): () => void {
  if (typeof window === 'undefined') {
    return () => undefined;
  }
  const message = options.message ?? DEFAULT_MESSAGE;
  const listener = (event: BeforeUnloadEvent): void => {
    if (!isDirty()) {
      return;
    }
    event.preventDefault();
    event.returnValue = message;
  };
  window.addEventListener('beforeunload', listener);
  return () => {
    window.removeEventListener('beforeunload', listener);
  };
}

export function blockNavigationIfDirty(
  navigation: BeforeNavigate,
  isDirty: () => boolean,
  options: UnsavedChangesOptions = {}
): void {
  if (!isDirty()) {
    return;
  }
  const shouldLeave = window.confirm(options.message ?? DEFAULT_MESSAGE);
  if (!shouldLeave) {
    navigation.cancel();
  }
}
