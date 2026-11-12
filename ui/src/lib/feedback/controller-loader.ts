let pending: Promise<typeof import('./state')> | null = null;
export function loadFeedbackController() {
  return pending ??= import('./state').catch(error => { pending = null; throw error; });
}
