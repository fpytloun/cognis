import type { PreprocessorGroup } from 'svelte/compiler';

export function isRichDocument(filename: string | undefined): boolean;
export function usesInjectedRichStyles(filename: string): boolean;
export const richDocumentStyles: PreprocessorGroup;
