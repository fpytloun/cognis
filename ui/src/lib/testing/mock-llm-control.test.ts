import { ChildProcess } from 'node:child_process';
import { PassThrough } from 'node:stream';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { requestMockBarrier } from './mock-llm-control';

const { execStub } = vi.hoisted(() => ({ execStub: vi.fn() }));
vi.mock('node:child_process', async (original) => {
  const actual = await original<typeof import('node:child_process')>();
  return { ...actual, default: { ...actual, execFile: execStub }, execFile: execStub };
});

const CONTAINER_ID = 'f'.repeat(64);
type Options = { signal?: AbortSignal; timeout: number; maxBuffer: number };
type Callback = (error: Error | null, stdout: string, stderr: string) => void;

function containerResponse(result: unknown, error: Error | null = null): { body: () => string } {
  let body = '';
  execStub.mockImplementation((_file: string, _args: string[], _options: Options, callback: Callback) => {
    const child = new ChildProcess();
    child.stdin = new PassThrough();
    child.stdin.on('data', (chunk: Buffer) => { body += chunk.toString(); });
    child.stdin.on('finish', () => callback(error, JSON.stringify(result), ''));
    return child;
  });
  return { body: () => body };
}

describe('mock barrier control transport', () => {
  beforeEach(() => {
    vi.stubEnv('COGNIS_E2E_MOCK_LLM_CONTROL', '');
    vi.stubEnv('COGNIS_E2E_MOCK_LLM_CONTAINER_ID', '');
    execStub.mockReset();
  });
  afterEach(() => {
    vi.unstubAllEnvs();
    vi.unstubAllGlobals();
  });

  it('retains normal host HTTP without container discovery', async () => {
    const response = new Response('{}', { status: 201 });
    const fetcher = vi.fn().mockResolvedValue(response);
    vi.stubGlobal('fetch', fetcher);
    const init = { method: 'POST', body: '{"turn_index":0}' };
    expect(await requestMockBarrier('http://127.0.0.1:8090', init)).toBe(response);
    expect(fetcher).toHaveBeenCalledWith(new URL('http://127.0.0.1:8090/__mock/barrier'), init);
    expect(execStub).not.toHaveBeenCalled();
  });

  it('uses only the explicit container and its actual loopback port', async () => {
    vi.stubEnv('COGNIS_E2E_MOCK_LLM_CONTROL', 'container');
    vi.stubEnv('COGNIS_E2E_MOCK_LLM_CONTAINER_ID', CONTAINER_ID);
    const transport = containerResponse({ status: 201, body: '{"barrier_id":"fixture-barrier"}' });
    const response = await requestMockBarrier('http://127.0.0.1:38690', {
      method: 'POST', body: '{"scenario_id":"fixture-scenario","turn_index":0}',
    });
    expect(response.status).toBe(201);
    expect(await response.json()).toEqual({ barrier_id: 'fixture-barrier' });
    expect(JSON.parse(transport.body())).toEqual({
      method: 'POST', body: '{"scenario_id":"fixture-scenario","turn_index":0}',
    });
    const [command, args, options] = execStub.mock.calls[0];
    expect(command).toBe('docker');
    expect(args.slice(0, 5)).toEqual(['exec', '-i', CONTAINER_ID, 'python', '-c']);
    expect(args[5]).toContain('HTTPConnection("127.0.0.1", 8090');
    expect(args[5]).toContain('signal.setitimer(signal.ITIMER_REAL, budget)');
    expect(args[6]).toBe('9000');
    expect(options.timeout).toBe(9500);
  });

  it('preserves an empty204 release response', async () => {
    vi.stubEnv('COGNIS_E2E_MOCK_LLM_CONTROL', 'container');
    vi.stubEnv('COGNIS_E2E_MOCK_LLM_CONTAINER_ID', CONTAINER_ID);
    containerResponse({ status: 204, body: '' });
    const response = await requestMockBarrier('http://127.0.0.1:38690', { method: 'DELETE' });
    expect(response.status).toBe(204);
    expect(await response.text()).toBe('');
  });

  it('retains the shorter cleanup deadline inside the container', async () => {
    vi.stubEnv('COGNIS_E2E_MOCK_LLM_CONTROL', 'container');
    vi.stubEnv('COGNIS_E2E_MOCK_LLM_CONTAINER_ID', CONTAINER_ID);
    containerResponse({ status: 204, body: '' });
    await requestMockBarrier('http://127.0.0.1:38690', { method: 'DELETE' }, 4500);
    expect(execStub.mock.calls[0][1][6]).toBe('4000');
    expect(execStub.mock.calls[0][2].timeout).toBe(4500);
  });

  it.each([403, 409, 410])('preserves barrier HTTP%s without fallback or retry', async (status) => {
    const fetcher = vi.fn();
    vi.stubGlobal('fetch', fetcher);
    vi.stubEnv('COGNIS_E2E_MOCK_LLM_CONTROL', 'container');
    vi.stubEnv('COGNIS_E2E_MOCK_LLM_CONTAINER_ID', CONTAINER_ID);
    const body = { detail: 'Control unavailable' };
    containerResponse({ status, body: JSON.stringify(body) });
    const response = await requestMockBarrier('http://127.0.0.1:38690');
    expect(response.status).toBe(status);
    expect(await response.json()).toEqual(body);
    expect(execStub).toHaveBeenCalledTimes(1);
    expect(fetcher).not.toHaveBeenCalled();
  });

  it('rejects oversized container requests before dispatch', async () => {
    const fetcher = vi.fn();
    vi.stubGlobal('fetch', fetcher);
    vi.stubEnv('COGNIS_E2E_MOCK_LLM_CONTROL', 'container');
    vi.stubEnv('COGNIS_E2E_MOCK_LLM_CONTAINER_ID', CONTAINER_ID);
    await expect(requestMockBarrier('http://127.0.0.1:38690', {
      method: 'POST', body: JSON.stringify({ value: 'x'.repeat(16_384) }),
    })).rejects.toThrow('exceeds limit');
    expect(execStub).not.toHaveBeenCalled();
    expect(fetcher).not.toHaveBeenCalled();
  });

  it.each(['', 'container-name', `${CONTAINER_ID};`])('rejects unbound identity %j before dispatch', async (identity) => {
    vi.stubEnv('COGNIS_E2E_MOCK_LLM_CONTROL', 'container');
    vi.stubEnv('COGNIS_E2E_MOCK_LLM_CONTAINER_ID', identity);
    const fetcher = vi.fn();
    vi.stubGlobal('fetch', fetcher);
    await expect(requestMockBarrier('http://127.0.0.1:38690')).rejects.toThrow('identity');
    expect(fetcher).not.toHaveBeenCalled();
    expect(execStub).not.toHaveBeenCalled();
  });

  it('does not fall back to HTTP after container failure', async () => {
    vi.stubEnv('COGNIS_E2E_MOCK_LLM_CONTROL', 'container');
    vi.stubEnv('COGNIS_E2E_MOCK_LLM_CONTAINER_ID', CONTAINER_ID);
    const fetcher = vi.fn();
    vi.stubGlobal('fetch', fetcher);
    containerResponse({}, new Error('transport failure'));
    await expect(requestMockBarrier('http://127.0.0.1:38690')).rejects.toThrow('transport failed');
    expect(fetcher).not.toHaveBeenCalled();
    expect(execStub).toHaveBeenCalledTimes(1);
  });

  it.each([{ status: 200 }, { status: '200', body: '{}' }, { status: 700, body: '{}' }])(
    'rejects malformed response %j',
    async (result) => {
      vi.stubEnv('COGNIS_E2E_MOCK_LLM_CONTROL', 'container');
      vi.stubEnv('COGNIS_E2E_MOCK_LLM_CONTAINER_ID', CONTAINER_ID);
      containerResponse(result);
      await expect(requestMockBarrier('http://127.0.0.1:38690')).rejects.toThrow('Invalid mock barrier response');
    },
  );

  it('rejects a pre-cancelled request without dispatch', async () => {
    const controller = new AbortController();
    controller.abort();
    await expect(requestMockBarrier('http://127.0.0.1:8090', { signal: controller.signal })).rejects.toThrow();
    expect(execStub).not.toHaveBeenCalled();
  });

  it('forwards cancellation to the owned child without retry', async () => {
    vi.stubEnv('COGNIS_E2E_MOCK_LLM_CONTROL', 'container');
    vi.stubEnv('COGNIS_E2E_MOCK_LLM_CONTAINER_ID', CONTAINER_ID);
    const controller = new AbortController();
    execStub.mockImplementation((_file: string, _args: string[], options: Options, callback: Callback) => {
      expect(options.signal).toBe(controller.signal);
      options.signal?.addEventListener('abort', () => callback(new Error('cancelled'), '', ''), { once: true });
      const child = new ChildProcess();
      child.stdin = new PassThrough();
      return child;
    });
    const pending = requestMockBarrier('http://127.0.0.1:38690', { signal: controller.signal });
    controller.abort();
    await expect(pending).rejects.toMatchObject({ name: 'AbortError' });
    expect(execStub).toHaveBeenCalledTimes(1);
  });

  it.each(['https://127.0.0.1:8090', 'http://remote.example:8090', 'http://user:secret@127.0.0.1:8090'])(
    'rejects inappropriate control origin %s',
    async (origin) => {
      await expect(requestMockBarrier(origin)).rejects.toThrow('loopback origin');
      expect(execStub).not.toHaveBeenCalled();
    },
  );
});
