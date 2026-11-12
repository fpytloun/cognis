import { describe, expect, it } from 'vitest';
import { pythonExecutionOutput } from './python-execution';

describe('pythonExecutionOutput', () => {
  it('uses incremental output while a cell is still running', () => {
    expect(pythonExecutionOutput({
      status: 'running', result: 'first\nsecond\n', streamedOutput: 'first\nsecond\n',
    })).toMatchObject({ output: 'first\nsecond\n', result: null });
  });

  it('retains terminal stdout and the emitted value without exposing the envelope as the result', () => {
    const result = JSON.stringify({
      protocol_version: 'cognis.program.v1', stdout_preview: 'first\nstderr\nlast\n', result: { count: 3 },
    });
    expect(pythonExecutionOutput({ status: 'completed', result, streamedOutput: result }))
      .toEqual({ output: 'first\nstderr\nlast\n', result: '{\n  "count": 3\n}', error: null, envelope: true });
  });

  it('retains output alongside a bounded execution error', () => {
    expect(pythonExecutionOutput({
      status: 'failed', result: JSON.stringify({
        protocol_version: 'cognis.program.v1', stdout_preview: 'before error\n',
        error: { bounded_message: 'Nested outcome is uncertain.' },
      }),
    })).toMatchObject({ output: 'before error\n', error: 'Nested outcome is uncertain.', envelope: true });
  });

  it('keeps truncated protocol data behind raw data instead of displaying it as the main result', () => {
    expect(pythonExecutionOutput({
      status: 'completed', result: '{"protocol_version":"cognis.program.v1","stdout_preview":"partial',
      streamedOutput: 'retained live output\n',
    })).toMatchObject({ output: 'retained live output\n', result: null, envelope: true });
  });

  it('does not reinterpret unrelated JSON or pre-protocol errors', () => {
    expect(pythonExecutionOutput({ status: 'completed', result: '{"value":3}' }))
      .toMatchObject({ result: '{"value":3}', envelope: false });
    expect(pythonExecutionOutput({ status: 'failed', result: 'Executor unavailable.' }))
      .toMatchObject({ result: 'Executor unavailable.', envelope: false });
  });
});
