import { getContext, setContext } from 'svelte';
import type { Component } from 'svelte';
import { documentCoordination, type DocumentLease } from './document-coordination';

export const DOCUMENT_SHARING_CONTEXT = Symbol('document-sharing');
const PEOPLE_LOADER_CONTEXT = Symbol('document-people-loader');
type PeopleLoader = NonNullable<DocumentSharingContext['loadPeople']>;
export interface DocumentSharingContext {
  readonly contentRef: string | null;
  readonly canManage: boolean;
  capture: () => DocumentLease;
  loadPeople?: () => Promise<{ default: Component<{ contentRef: string; onBusy?: (value: boolean) => void }> }>;
}
/** Human sharing uses document metadata, independently of annotation support. */
export function provideDocumentSharing(value: DocumentSharingContext): void {
  setContext(DOCUMENT_SHARING_CONTEXT, value);
}
export function getDocumentSharing(): DocumentSharingContext | undefined {
  return getContext<DocumentSharingContext | undefined>(DOCUMENT_SHARING_CONTEXT);
}
/** The app injects private UI; portable exports never import the SPA API client. */
export function provideDocumentPeopleLoader(loader: PeopleLoader): void {
  setContext(PEOPLE_LOADER_CONTEXT, loader);
}
export function getDocumentPeopleLoader(): PeopleLoader | undefined {
  return getContext<PeopleLoader | undefined>(PEOPLE_LOADER_CONTEXT);
}
export function captureSharingLease(context: DocumentSharingContext | undefined): DocumentLease {
  return context ? context.capture() : documentCoordination().capture();
}
