import { constants } from 'node:fs';
import { lstat, open, access } from 'node:fs/promises';
import { isAbsolute } from 'node:path';
import { createHash } from 'node:crypto';

const EXECUTABLE_PATH_ENV = 'COGNIS_E2E_CHROMIUM_EXECUTABLE_PATH';
const SHA256_ENV = 'COGNIS_E2E_CHROMIUM_SHA256';
const SHA256_PATTERN = /^[a-f\d]{64}$/i;
const HASH_CHUNK_SIZE = 64 * 1024;

/**
 * Return an explicitly verified Chromium path, or undefined when not opted in.
 * Supplying either environment variable opts in and requires the complete pair.
 *
 * @param {NodeJS.ProcessEnv} env
 * @returns {Promise<string | undefined>}
 */
export async function verifiedChromiumExecutablePath(env = process.env) {
  const executablePath = env[EXECUTABLE_PATH_ENV];
  const expectedSha256 = env[SHA256_ENV];

  if (executablePath === undefined && expectedSha256 === undefined) return undefined;
  if (executablePath === undefined || expectedSha256 === undefined) {
    throw new Error(`${EXECUTABLE_PATH_ENV} and ${SHA256_ENV} must be set together`);
  }
  if (!isAbsolute(executablePath)) {
    throw new Error(`${EXECUTABLE_PATH_ENV} must be an absolute path`);
  }
  if (!SHA256_PATTERN.test(expectedSha256)) {
    throw new Error(`${SHA256_ENV} must contain a 64-character SHA-256 digest`);
  }

  let file;
  try {
    if (typeof constants.O_NOFOLLOW !== 'number' || typeof constants.O_NONBLOCK !== 'number') {
      throw new Error('the platform cannot securely reject symlink and blocking special-file inputs');
    }
    const pathMetadata = await lstat(executablePath);
    if (!pathMetadata.isFile() || (pathMetadata.mode & 0o444) === 0 || (pathMetadata.mode & 0o111) === 0) {
      throw new Error('must be a readable regular executable file');
    }
    file = await open(executablePath, constants.O_RDONLY | constants.O_NOFOLLOW | constants.O_NONBLOCK);
    const metadata = await file.stat();
    if (!metadata.isFile() || (metadata.mode & 0o444) === 0 || (metadata.mode & 0o111) === 0) {
      throw new Error('must be a readable regular executable file');
    }
    await access(executablePath, constants.R_OK | constants.X_OK);

    const hash = createHash('sha256');
    const chunk = Buffer.allocUnsafe(HASH_CHUNK_SIZE);
    let position = 0;
    while (true) {
      const { bytesRead } = await file.read(chunk, 0, chunk.length, position);
      if (bytesRead === 0) break;
      hash.update(chunk.subarray(0, bytesRead));
      position += bytesRead;
    }

    if (hash.digest('hex') !== expectedSha256.toLowerCase()) {
      throw new Error(`${SHA256_ENV} does not match the executable bytes`);
    }
  } catch (error) {
    const detail = error instanceof Error ? error.message : String(error);
    throw new Error(`Cannot use ${EXECUTABLE_PATH_ENV}: ${detail}`, { cause: error });
  } finally {
    await file?.close();
  }

  return executablePath;
}

/**
 * Build the Playwright use override without changing defaults when not opted in.
 *
 * @param {NodeJS.ProcessEnv} env
 * @returns {Promise<{ launchOptions?: { executablePath: string } }>}
 */
export async function verifiedChromiumLaunchOptions(env = process.env) {
  const executablePath = await verifiedChromiumExecutablePath(env);
  return executablePath === undefined ? {} : { launchOptions: { executablePath } };
}
