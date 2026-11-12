// One locked, serial capture run owns the local fixture server and its cleanup.
import { spawn } from 'node:child_process';
import { once } from 'node:events';
import { fileURLToPath } from 'node:url';
const cwd = fileURLToPath(new URL('../../', import.meta.url));
const server = spawn(process.execPath, ['node_modules/vite/bin/vite.js', '--config', 'vite.usage.config.ts'], { cwd, stdio: ['ignore', 'pipe', 'pipe'] });
try {
  await new Promise((resolve, reject) => {
    server.once('error', reject);
    server.once('exit', (code) => reject(new Error(`Task-owned fixture server exited before readiness (${code})`)));
    server.stdout.on('data', (chunk) => { process.stdout.write(chunk); if (chunk.toString().includes('127.0.0.1:5197')) resolve(); });
    server.stderr.on('data', (chunk) => process.stderr.write(chunk));
  });
  await import('./capture.mjs');
} finally {
  if (server.exitCode === null && server.signalCode === null) {
    const stopped = once(server, 'exit');
    server.kill('SIGTERM');
    await stopped;
  }
}
