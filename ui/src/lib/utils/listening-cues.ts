/** Quiet paired notes, scoped to one conversation overlay and never mixed into microphone input. */
export class ListeningCues {
  private context: AudioContext | null = null;
  private cancel: (() => void) | null = null;

  unlock(): void {
    try {
      const Constructor = window.AudioContext ??
        (window as unknown as { webkitAudioContext?: typeof AudioContext }).webkitAudioContext;
      if (!Constructor) return;
      if (!this.context || this.context.state === 'closed') this.context = new Constructor();
      if (this.context.state === 'suspended') void this.context.resume().catch(() => {});
    } catch {
      // Optional cues must not prevent microphone use on a restricted audio device.
    }
  }

  play(kind: 'start' | 'stop' | 'waiting'): Promise<void> {
    this.cancel?.();
    this.unlock();
    const context = this.context;
    if (!context) return Promise.resolve();
    return new Promise(resolve => {
      let settled = false;
      const nodes: OscillatorNode[] = [];
      const gains: GainNode[] = [];
      let settleTimer: ReturnType<typeof setTimeout> | undefined;
      const finish = () => {
        if (settled) return;
        settled = true;
        clearTimeout(deadline);
        clearTimeout(settleTimer);
        for (const node of nodes) {
          node.onended = null;
          try { node.stop(); } catch { /* The note may already have ended. */ }
          node.disconnect();
        }
        for (const gain of gains) gain.disconnect();
        if (this.cancel === finish) this.cancel = null;
        resolve();
      };
      // Autoplay permission can leave resume pending. Never leave capture muted indefinitely.
      const deadline = setTimeout(finish, 400);
      this.cancel = finish;
      const schedule = () => {
        if (settled) return;
        try {
          const frequencies = kind === 'start' ? [520, 660] : kind === 'stop' ? [660, 520] : [440];
          const start = context.currentTime + 0.008;
          frequencies.forEach((frequency, index) => {
            const node = context.createOscillator();
            const gain = context.createGain();
            nodes.push(node); gains.push(gain);
            node.type = 'sine';
            const at = start + index * 0.065;
            node.frequency.setValueAtTime(frequency, at);
            gain.gain.setValueAtTime(0.0001, at);
            gain.gain.exponentialRampToValueAtTime(kind === 'waiting' ? 0.07 : 0.12, at + 0.008);
            gain.gain.exponentialRampToValueAtTime(0.0001, at + 0.055);
            node.connect(gain); gain.connect(context.destination);
            if (index === frequencies.length - 1) node.onended = () => {
              const latency = (context.baseLatency || 0) + (context.outputLatency || 0);
              settleTimer = setTimeout(finish, Math.min(150, latency * 1000 + 25));
            };
            node.start(at); node.stop(at + 0.055);
          });
        } catch {
          finish();
        }
      };
      if (context.state === 'running') schedule();
      else void context.resume().then(schedule, finish);
    });
  }

  stop(): void {
    this.cancel?.();
  }

  dispose(): void {
    this.stop();
    const context = this.context;
    this.context = null;
    if (context && context.state !== 'closed') void context.close().catch(() => {});
  }
}
