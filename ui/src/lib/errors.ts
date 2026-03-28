import { toErrorMessage } from '$lib/utils';

export function reportError(
  message: string,
  error: unknown,
  context: Record<string, unknown> = {}
): void {
  console.error('[cognis-ui]', message, {
    error: toErrorMessage(error),
    context
  });
}
