/* Mono PCM capture with a continuous, block-boundary-safe rate converter. */
class PcmCapture extends AudioWorkletProcessor {
  constructor(options) {
    super();
    this.ratio = sampleRate / options.processorOptions.sampleRate;
    this.sum = 0;
    this.weight = 0;
    this.samples = new Int16Array(4096);
    this.count = 0;
    this.stopped = false;
    this.port.onmessage = () => {
      this.stopped = true;
      this.flush();
      this.port.postMessage({ type: 'flushed' });
    };
  }

  flush() {
    if (!this.count) return;
    const frame = this.samples.slice(0, this.count);
    this.port.postMessage(frame.buffer, [frame.buffer]);
    this.count = 0;
  }

  process(inputs) {
    if (this.stopped) return false;
    const channels = inputs[0];
    if (!channels?.length) return true;
    for (let i = 0; i < channels[0].length; i++) {
      let value = 0;
      for (const channel of channels) value += channel[i] / channels.length;
      let remaining = 1;
      while (remaining > 1e-8) {
        const take = Math.min(remaining, this.ratio - this.weight);
        this.sum += value * take;
        this.weight += take;
        remaining -= take;
        if (this.weight >= this.ratio - 1e-8) {
          const sample = Math.max(-1, Math.min(1, this.sum / this.ratio));
          this.samples[this.count++] = Math.round(sample * (sample < 0 ? 32768 : 32767));
          this.sum = this.weight = 0;
          if (this.count === this.samples.length) this.flush();
        }
      }
    }
    return true;
  }
}

registerProcessor('cognis-pcm-capture', PcmCapture);
