export type RequestTicket = {
  generation: number;
  controller: AbortController;
};

export class LatestRequestGate {
  #generation = 0;
  #current: RequestTicket | null = null;

  start(): RequestTicket {
    this.invalidate();
    const ticket = {
      generation: this.#generation,
      controller: new AbortController(),
    };
    this.#current = ticket;
    return ticket;
  }

  invalidate(): void {
    this.#generation += 1;
    this.#current?.controller.abort();
    this.#current = null;
  }

  isCurrent(ticket: RequestTicket): boolean {
    return (
      !ticket.controller.signal.aborted
      && ticket.generation === this.#generation
      && this.#current === ticket
    );
  }

  finish(ticket: RequestTicket): boolean {
    if (!this.isCurrent(ticket)) return false;
    this.#current = null;
    return true;
  }
}

export function searchResetAction(
  currentQuery: string,
  pendingReset: boolean,
  nextInput: string,
): 'none' | 'invalidate' | 'reschedule' | 'restore' {
  if (nextInput.trim() !== currentQuery) return pendingReset ? 'reschedule' : 'invalidate';
  return pendingReset ? 'restore' : 'none';
}
