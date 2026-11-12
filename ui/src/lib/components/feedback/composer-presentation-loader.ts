/** Only executable presentation is shared; each tray retains its own data and authority. */
export function loadComposerPresentation() {
  return import('./composer-presentation');
}
