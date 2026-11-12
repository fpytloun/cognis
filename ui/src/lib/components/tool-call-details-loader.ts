type DetailsComponent = typeof import('./ToolCallDetails.svelte').default;
let component: DetailsComponent | null = null;
let pending: Promise<DetailsComponent> | null = null;

export function peekToolCallDetails(): DetailsComponent | null {
  return component;
}

export function loadToolCallDetails(): Promise<DetailsComponent> {
  if (component) return Promise.resolve(component);
  return pending ??= import('./ToolCallDetails.svelte').then(module => {
    component = module.default;
    return component;
  }).catch(error => {
    pending = null;
    throw error;
  });
}
