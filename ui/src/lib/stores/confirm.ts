import { writable } from 'svelte/store';

import { createId } from '$lib/utils';

export interface ConfirmOptions {
  title: string;
  message: string;
  confirmLabel?: string;
  cancelLabel?: string;
  variant?: 'danger' | 'primary';
}

export interface ConfirmRequest extends ConfirmOptions {
  id: string;
}

const state = writable<ConfirmRequest | null>(null);
let resolver: ((value: boolean) => void) | null = null;

export function confirmAction(options: ConfirmOptions): Promise<boolean> {
  if (resolver) {
    resolver(false);
  }
  state.set({
    id: createId('confirm'),
    confirmLabel: 'Confirm',
    cancelLabel: 'Cancel',
    variant: 'danger',
    ...options
  });
  return new Promise<boolean>((resolve) => {
    resolver = resolve;
  });
}

export function resolveConfirm(value: boolean): void {
  state.set(null);
  resolver?.(value);
  resolver = null;
}

export const confirmStore = {
  subscribe: state.subscribe
};
