import { getWebSocketUrl } from '$lib/config';
import { documentCoordination, type DocumentLease } from '$lib/document-coordination';

const MAX_PENDING_FRAMES = 96;

/** One connection-owned microphone utterance. Never reconnect or replay it. */
export class LiveTranscription {
  private socket: WebSocket;
  private source: MediaStreamAudioSourceNode;
  private node: AudioWorkletNode;
  private buffered: string[] = [];
  private ready = false;
  private closed = false;
  private text: string | null = null;
  private requestId = crypto.randomUUID();
  private resolveReady!: () => void;
  private rejectReady!: (error: Error) => void;
  private resolveResult!: (result: { text: string }) => void;
  private rejectResult!: (error: Error) => void;
  private resolveFlushed!: () => void;
  private readyPromise = new Promise<void>((resolve, reject) => {
    this.resolveReady = resolve; this.rejectReady = reject;
  });
  private result = new Promise<{ text: string }>((resolve, reject) => {
    this.resolveResult = resolve; this.rejectResult = reject;
  });
  private flushed = new Promise<void>((resolve) => { this.resolveFlushed = resolve; });
  private timer: ReturnType<typeof setTimeout>;
  private abort: () => void;
  private sendTail = Promise.resolve();
  private receiveTail = Promise.resolve();
  private pendingSends = 0;
  private pendingReceives = 0;
  private committing = false;
  private captureFlushed = false;

  static supported(): boolean {
    return typeof AudioContext !== 'undefined' && typeof AudioWorkletNode !== 'undefined';
  }

  private constructor(
    private context: AudioContext, stream: MediaStream, sampleRate: number,
    private signal: AbortSignal, private lease: DocumentLease, onPartial: (text: string) => void,
    private onFailure: () => void,
  ) {
    this.abort = () => this.cancel();
    signal.addEventListener('abort', this.abort, { once: true });
    this.source = context.createMediaStreamSource(stream);
    this.node = new AudioWorkletNode(context, 'cognis-pcm-capture', {
      processorOptions: { sampleRate }, channelCount: 1,
    });
    this.source.connect(this.node);
    this.node.connect(context.destination);
    this.socket = new WebSocket(getWebSocketUrl());
    this.node.port.onmessage = ({ data }) => {
      if (data?.type === 'flushed') { this.captureFlushed = true; this.resolveFlushed(); return; }
      if (this.closed || this.captureFlushed || !(data instanceof ArrayBuffer)) return;
      const bytes = new Uint8Array(data);
      let raw = '';
      for (const value of bytes) raw += String.fromCharCode(value);
      const encoded = btoa(raw);
      if (this.ready) this.sendAudio(encoded);
      else {
        this.buffered.push(encoded);
        if (this.buffered.length > MAX_PENDING_FRAMES) this.fail(new Error('Microphone startup buffer exceeded'));
      }
    };
    this.socket.onmessage = ({ data }) => {
      if (this.closed) return;
      if (++this.pendingReceives > MAX_PENDING_FRAMES) {
        this.fail(new Error('Transcription event buffer exceeded'));
        return;
      }
      this.receiveTail = this.receiveTail.then(async () => {
        if (this.closed) return;
        await this.lease.validate();
        if (this.closed) return;
        this.signal.throwIfAborted();
        const message = JSON.parse(data);
        if (message.type === 'authenticated') {
          await this.send({ type: 'speech_stt_start' });
        } else if (message.request_id === this.requestId && message.type === 'speech_stt_error') {
          this.fail(new Error('Live transcription failed'));
        } else if (message.request_id === this.requestId && message.type === 'speech_stt_event') {
          const event = message.event;
          if (event.type === 'start') {
            if (event.sample_rate !== sampleRate) throw new Error('Speech configuration changed; retry the call');
            this.ready = true;
            clearTimeout(this.timer);
            for (const frame of this.buffered.splice(0)) this.sendAudio(frame);
            this.resolveReady();
          } else if (event.type === 'partial') {
            onPartial(String(event.text));
          } else if (event.type === 'transcript') {
            this.text = String(event.text);
          } else if (event.type === 'done') {
            if (this.text === null) throw new Error('Transcription ended without a transcript');
            this.resolveResult({ text: this.text });
            this.release();
          }
        }
      }).catch(error => this.fail(error instanceof Error ? error : new Error('Invalid speech event')))
        .finally(() => { this.pendingReceives--; });
    };
    this.socket.onerror = () => this.fail(new Error('Live transcription connection failed'));
    this.socket.onclose = () => {
      void this.receiveTail.then(() => {
        if (!this.closed) this.fail(new Error('Live transcription disconnected'));
      });
    };
    this.timer = setTimeout(() => this.fail(new Error('Live transcription startup timed out')), 15_000);
    // The result is consumed at commit; retain failures without an unhandled rejection meanwhile.
    void this.result.catch(() => {});
    if (signal.aborted) this.cancel();
  }

  static async open(
    stream: MediaStream, sampleRate: number, signal: AbortSignal,
    onPartial: (text: string) => void, onFailure: () => void,
  ): Promise<LiveTranscription> {
    const lease = documentCoordination().capture();
    signal = AbortSignal.any([signal, lease.signal]);
    const context = new AudioContext();
    try {
      await lease.validate();
      signal.throwIfAborted();
      await context.audioWorklet.addModule('/audio/pcm-capture.js');
      await context.resume();
      await lease.validate();
      if (signal.aborted) throw new DOMException('Aborted', 'AbortError');
      const session = new LiveTranscription(context, stream, sampleRate, signal, lease, onPartial, onFailure);
      await session.readyPromise;
      return session;
    } catch (error) {
      if (context.state !== 'closed') void context.close().catch(() => {});
      throw error;
    }
  }

  private send(message: Record<string, unknown>): Promise<void> {
    if (this.closed) return Promise.reject(new DOMException('Aborted', 'AbortError'));
    if (this.pendingSends >= MAX_PENDING_FRAMES) return Promise.reject(new Error('Microphone validation buffer exceeded'));
    this.pendingSends++;
    const sent = this.sendTail.then(async () => {
      if (this.closed) throw new DOMException('Aborted', 'AbortError');
      await this.lease.validate();
      if (this.closed) throw new DOMException('Aborted', 'AbortError');
      this.signal.throwIfAborted();
      if (this.socket.readyState !== WebSocket.OPEN || this.socket.bufferedAmount > 262_144) {
        throw new Error('Microphone connection cannot keep up');
      }
      this.socket.send(JSON.stringify({ ...message, request_id: this.requestId }));
    });
    this.sendTail = sent.catch(() => {});
    return sent.finally(() => { this.pendingSends--; });
  }

  private sendAudio(encoded: string): void {
    void this.send({ type: 'speech_stt_audio', audio_base64: encoded }).catch(error => this.fail(error));
  }

  private fail(error: Error): void {
    if (this.closed) return;
    const notify = this.ready && !this.signal.aborted && error.name !== 'AbortError';
    this.rejectReady(error);
    this.rejectResult(error);
    this.release();
    if (notify) this.onFailure();
  }

  private release(): void {
    this.closed = true;
    clearTimeout(this.timer);
    this.signal.removeEventListener('abort', this.abort);
    this.resolveFlushed();
    this.source.disconnect();
    this.node.disconnect();
    this.buffered = [];
    if (this.context.state !== 'closed') void this.context.close().catch(() => {});
    this.socket.close();
  }

  cancel(): void {
    if (this.closed) return;
    this.fail(new DOMException('Aborted', 'AbortError'));
  }

  async commit(): Promise<{ text: string }> {
    if (this.closed || this.committing) return this.result;
    this.committing = true;
    this.timer = setTimeout(() => this.fail(new Error('Transcription commit timed out')), 30_000);
    try {
      this.source.disconnect();
      await Promise.race([this.sendTail, this.result]);
      if (this.closed) return this.result;
      const timeout = setTimeout(() => this.fail(new Error('Microphone flush timed out')), 2_000);
      try {
        this.node.port.postMessage({ type: 'flush' });
        await this.flushed;
      } finally {
        clearTimeout(timeout);
      }
      if (this.closed) return this.result;
      this.node.disconnect();
      await Promise.race([this.sendTail, this.result]);
      if (!this.closed) await Promise.race([this.send({ type: 'speech_stt_commit' }), this.result]);
    } catch (error) {
      this.fail(error instanceof Error ? error : new Error('Transcription commit failed'));
    }
    return this.result;
  }
}
