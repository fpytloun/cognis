const forbiddenModulePatterns = [
  /\/src\/routes\//,
  /\/src\/lib\/stores\/auth/,
  /\/src\/lib\/stores\/chat/,
  /\/node_modules\/@sveltejs\/kit\//,
];

export function standaloneDependencyViolation(
  moduleIds: string[],
  options: { allowLazyHeavy?: boolean } = {},
): string | null {
  const forbidden = moduleIds.find((moduleId) =>
    forbiddenModulePatterns.some((pattern) => pattern.test(moduleId)));
  if (forbidden) return `forbidden SPA dependency: ${forbidden}`;
  const eagerHeavyDependency = options.allowLazyHeavy
    ? undefined
    : moduleIds.find((moduleId) => /\/node_modules\/(?:chart\.js|mermaid)\//.test(moduleId));
  return eagerHeavyDependency ? `eagerly included lazy dependency: ${eagerHeavyDependency}` : null;
}
