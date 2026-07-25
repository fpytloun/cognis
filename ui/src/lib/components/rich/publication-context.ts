import { getContext, setContext } from 'svelte';
import { writable, type Writable } from 'svelte/store';

import type { CitationRegistry } from './publication';

const PUBLICATION_CONTEXT = Symbol('rich-publication-context');
const EMPTY_REGISTRY: CitationRegistry = { numbers: {}, sources: [], namespace: '' };

export function createPublicationContext(): Writable<CitationRegistry> {
  const store = writable<CitationRegistry>(EMPTY_REGISTRY);
  setContext(PUBLICATION_CONTEXT, store);
  return store;
}

export function getPublicationContext(): Writable<CitationRegistry> {
  return getContext<Writable<CitationRegistry>>(PUBLICATION_CONTEXT) ?? writable(EMPTY_REGISTRY);
}
