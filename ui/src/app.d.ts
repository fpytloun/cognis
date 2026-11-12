import type { UsageHistoryState } from '$lib/usage/query-state';
// See https://svelte.dev/docs/kit/types#app.d.ts
declare global {
  namespace App {
    // interface Error {}
    // interface Locals {}
    // interface PageData {}
    interface PageState extends UsageHistoryState {}
    // interface Platform {}
  }
}

export {};
