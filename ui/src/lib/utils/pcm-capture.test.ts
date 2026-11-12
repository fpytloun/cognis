import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { runInNewContext } from 'node:vm';
import { expect, it } from 'vitest';

it('resamples the shipped worklet continuously across microphone block boundaries', () => {
  const script = readFileSync(resolve('static/audio/pcm-capture.js'), 'utf8');
  function capture(blockSize: number): number[] {
    const frames: ArrayBuffer[] = [];
    let Processor!: new (options: { processorOptions: { sampleRate: number } }) => {
      process(input: Float32Array[][]): boolean;
      port: { onmessage: () => void };
    };
    runInNewContext(script, {
      sampleRate: 44100,
      AudioWorkletProcessor: class {
        port = { postMessage: (frame: unknown) => { if (frame instanceof ArrayBuffer) frames.push(frame); } };
      },
      ArrayBuffer, Int16Array,
      registerProcessor: (_name: string, processor: typeof Processor) => { Processor = processor; },
    });
    const processor = new Processor({ processorOptions: { sampleRate: 16000 } });
    const source = Float32Array.from({ length: 22050 }, (_, index) => Math.sin(index / 31) * 1.2);
    for (let index = 0; index < source.length; index += blockSize) {
      processor.process([[source.subarray(index, index + blockSize)]]);
    }
    processor.port.onmessage();
    return frames.flatMap(frame => Array.from(new Int16Array(frame)));
  }
  const blocked = capture(128);
  expect(blocked).toHaveLength(8000);
  expect(blocked).toEqual(capture(22050));
  expect(Math.min(...blocked)).toBeGreaterThanOrEqual(-32768);
  expect(Math.max(...blocked)).toBeLessThanOrEqual(32767);
});
