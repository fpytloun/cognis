import { createHash } from 'node:crypto';
import { mkdtemp, chmod, mkdir, symlink, writeFile, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { after, describe, it } from 'node:test';
import {
  verifiedChromiumExecutablePath,
  verifiedChromiumLaunchOptions
} from './verified-chromium.mjs';

const pathEnv = 'COGNIS_E2E_CHROMIUM_EXECUTABLE_PATH';
const hashEnv = 'COGNIS_E2E_CHROMIUM_SHA256';
const roots = [];

async function executable(content = '#!/bin/sh\nexit 0\n') {
  const root = await mkdtemp(join(tmpdir(), 'cognis-e2e-chromium-'));
  roots.push(root);
  const executablePath = join(root, 'chromium');
  await writeFile(executablePath, content, { mode: 0o700 });
  const sha256 = createHash('sha256').update(content).digest('hex');
  return { executablePath, sha256, env: { [pathEnv]: executablePath, [hashEnv]: sha256 } };
}

after(async () => {
  await Promise.all(roots.splice(0).map((root) => rm(root, { recursive: true, force: true })));
});

describe('verified native Chromium opt-in', () => {
  it('keeps the default path unset when both variables are absent', async () => {
    assert.equal(await verifiedChromiumExecutablePath({}), undefined);
    assert.deepEqual(await verifiedChromiumLaunchOptions({}), {});
  });

  it('returns the explicitly bound path only when its bytes match', async () => {
    const candidate = await executable();
    assert.equal(await verifiedChromiumExecutablePath(candidate.env), candidate.executablePath);
    assert.deepEqual(await verifiedChromiumLaunchOptions(candidate.env), {
      launchOptions: { executablePath: candidate.executablePath }
    });
  });

  const invalidInputs = [
    [{ [pathEnv]: '/absolute/browser' }, 'must be set together'],
    [{ [hashEnv]: 'a'.repeat(64) }, 'must be set together'],
    [{ [pathEnv]: 'relative/browser', [hashEnv]: 'a'.repeat(64) }, 'absolute path'],
    [{ [pathEnv]: '/absolute/browser', [hashEnv]: 'not-a-digest' }, '64-character'],
    [{ [pathEnv]: '/absolute/browser', [hashEnv]: 'a'.repeat(64) }, 'Cannot use'],
  ];
  for (const [index, [env, error]] of invalidInputs.entries()) {
    it(`rejects invalid opt-in input ${index + 1}`, async () => {
      await assert.rejects(verifiedChromiumExecutablePath(env), new RegExp(error));
    });
  }

  it('rejects a malformed digest rather than silently using the default', async () => {
    await assert.rejects(
      verifiedChromiumExecutablePath({ [pathEnv]: '/absolute/browser', [hashEnv]: 'a'.repeat(63) }),
      /64-character/
    );
  });

  it('rejects a final-component symlink', async () => {
    const candidate = await executable();
    const linkPath = join(roots[0], 'chromium-link');
    await symlink(candidate.executablePath, linkPath);
    await assert.rejects(verifiedChromiumExecutablePath({
      ...candidate.env,
      [pathEnv]: linkPath
    }), /Cannot use/);
  });

  it('rejects a FIFO without waiting for a writer', async () => {
    const candidate = await executable();
    const fifoPath = join(roots[0], 'chromium-fifo');
    execFileSync('mkfifo', [fifoPath]);
    await assert.rejects(verifiedChromiumExecutablePath({
      ...candidate.env,
      [pathEnv]: fifoPath
    }), /regular executable/);
  });

  it('rejects non-regular, unreadable, and non-executable files', async () => {
    const candidate = await executable();
    const directory = join(roots[0], 'directory');
    await mkdir(directory);
    await assert.rejects(verifiedChromiumExecutablePath({
      ...candidate.env,
      [pathEnv]: directory
    }), /readable regular executable/);

    await chmod(candidate.executablePath, 0o100);
    await assert.rejects(verifiedChromiumExecutablePath(candidate.env), /Cannot use|readable regular executable/);

    await chmod(candidate.executablePath, 0o600);
    await assert.rejects(verifiedChromiumExecutablePath(candidate.env), /readable regular executable/);
  });

  it('fails when the executable cannot be opened for reading', async () => {
    const candidate = await executable();
    await chmod(candidate.executablePath, 0o000);
    await assert.rejects(verifiedChromiumExecutablePath(candidate.env), /Cannot use/);
  });

  it('rejects files without execute permission', async () => {
    const candidate = await executable();
    await chmod(candidate.executablePath, 0o600);
    await assert.rejects(verifiedChromiumExecutablePath(candidate.env), /readable regular executable/);
  });

  it('rejects a digest mismatch', async () => {
    const candidate = await executable();
    await assert.rejects(verifiedChromiumExecutablePath({
      ...candidate.env,
      [hashEnv]: '0'.repeat(64)
    }), /does not match/);
  });
});
