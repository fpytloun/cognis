import { createEmptyAgentForm } from '$lib/agents';

export function createReactiveAgentForm() {
  const form = $state(createEmptyAgentForm());
  return form;
}
