export function verifiedChromiumExecutablePath(
  env?: NodeJS.ProcessEnv
): Promise<string | undefined>;
export function verifiedChromiumLaunchOptions(
  env?: NodeJS.ProcessEnv
): Promise<{ launchOptions?: { executablePath: string } }>;
