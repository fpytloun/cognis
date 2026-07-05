export interface WebSocketErrorLike {
  code?: unknown;
  message?: unknown;
}

export function isNonFatalWebSocketBackpressureError(event: WebSocketErrorLike): boolean {
  const code = typeof event.code === 'string' ? event.code.toLowerCase() : '';
  if (code === 'rate_limited') return true;

  const message = typeof event.message === 'string' ? event.message.toLowerCase() : '';
  return message.includes('too many websocket messages');
}
