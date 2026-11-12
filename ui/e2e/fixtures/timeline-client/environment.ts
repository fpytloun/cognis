export const browser = true;
export const dev = true;
export const version = 'local-fixture';
export const env = {};
export const base = '';
export const assets = '';
export function beforeNavigate(): void {}
export function afterNavigate(): void {}
export function goto(): never { throw new Error('Navigation is outside this synthetic fixture'); }
