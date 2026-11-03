import { execFile } from 'node:child_process';

const CONTROL_PATH = '/__mock/barrier';
const METHODS = new Set(['GET', 'POST', 'PUT', 'DELETE']);

// Docker's published port has a bridge peer. Control still runs on actual
// container loopback; the mock server's loopback-only predicate is unchanged.
const CONTAINER_REQUEST = `
import http.client
import json
import signal
import sys

def expired(signum, frame):
    raise TimeoutError("Mock barrier control deadline")

signal.signal(signal.SIGALRM, expired)
budget = float(sys.argv[1]) / 1000
signal.setitimer(signal.ITIMER_REAL, budget)
request = json.load(sys.stdin)
if request["method"] not in {"GET", "POST", "PUT", "DELETE"}:
    raise ValueError("Unsupported mock barrier method")
connection = http.client.HTTPConnection("127.0.0.1", 8090, timeout=budget)
try:
    connection.request(
        request["method"], "/__mock/barrier",
        body=request["body"], headers={"Content-Type": "application/json"},
    )
    response = connection.getresponse()
    body = response.read(65537)
    if len(body) > 65536:
        raise ValueError("Mock barrier response exceeds limit")
    print(json.dumps({"status": response.status, "body": body.decode("utf-8")}))
finally:
    connection.close()
    signal.setitimer(signal.ITIMER_REAL, 0)
`;

/** Use only the explicit launcher-owned mock container, or normal host HTTP. */
export async function requestMockBarrier(baseUrl: string, init: RequestInit = {}, timeoutMs = 9500): Promise<Response> {
  const origin = new URL(baseUrl);
  if (
    origin.protocol !== 'http:' ||
    !['localhost', '127.0.0.1', '[::1]'].includes(origin.hostname) ||
    origin.username || origin.password
  ) {
    throw new Error('Mock barrier control requires an uncredentialed loopback origin');
  }
  const method = (init.method ?? 'GET').toUpperCase();
  if (
    !METHODS.has(method) || (init.body != null && typeof init.body !== 'string') ||
    !Number.isInteger(timeoutMs) || timeoutMs < 1000 || timeoutMs > 9500
  ) {
    throw new Error('Unsupported mock barrier request');
  }
  init.signal?.throwIfAborted();
  const mode = process.env.COGNIS_E2E_MOCK_LLM_CONTROL;
  const container = process.env.COGNIS_E2E_MOCK_LLM_CONTAINER_ID;
  if (!mode && !container) {
    return fetch(new URL(CONTROL_PATH, origin), init);
  }
  if (mode !== 'container' || !container || !/^[a-f0-9]{64}$/.test(container)) {
    throw new Error('Mock barrier container identity is missing or invalid');
  }
  const request = JSON.stringify({ method, body: init.body ?? null });
  if (Buffer.byteLength(request) > 16_384) throw new Error('Mock barrier request exceeds limit');
  return new Promise<Response>((resolve, reject) => {
    const child = execFile(
      'docker', ['exec', '-i', container, 'python', '-c', CONTAINER_REQUEST, String(timeoutMs - 500)],
      { signal: init.signal ?? undefined, timeout: timeoutMs, maxBuffer: 262_144 },
      (error, stdout) => {
        if (error) {
          reject(init.signal?.aborted
            ? new DOMException('Mock barrier control cancelled', 'AbortError')
            : new Error(`Mock barrier container transport failed (code=${error.code ?? 'spawn'}, signal=${error.signal ?? 'none'})`));
          return;
        }
        try {
          const result: unknown = JSON.parse(stdout);
          if (
            typeof result !== 'object' || result === null ||
            !('status' in result) || !Number.isInteger(result.status) ||
            typeof result.status !== 'number' || result.status < 200 || result.status > 599 ||
            !('body' in result) || typeof result.body !== 'string'
          ) {
            throw new Error('Invalid mock barrier response');
          }
          resolve(new Response(result.status === 204 ? null : result.body, {
            status: result.status,
            headers: { 'content-type': 'application/json' },
          }));
        } catch {
          reject(new Error('Invalid mock barrier response'));
        }
      },
    );
    child.stdin?.on('error', () => reject(new Error('Mock barrier request transport failed')));
    if (!child.stdin) {
      child.kill();
      reject(new Error('Mock barrier request transport unavailable'));
      return;
    }
    child.stdin.end(request);
  });
}
