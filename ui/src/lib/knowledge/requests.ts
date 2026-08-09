export interface LatestRequestToken {
  generation: number;
  controller: AbortController;
}

export class LatestRequestController {
  #generation = 0;
  #controller: AbortController | null = null;

  begin(): LatestRequestToken {
    this.cancel();
    const controller = new AbortController();
    this.#controller = controller;
    return { generation: ++this.#generation, controller };
  }

  cancel(): void {
    this.#generation += 1;
    this.#controller?.abort();
    this.#controller = null;
  }

  isCurrent(token: LatestRequestToken): boolean {
    return token.generation === this.#generation && !token.controller.signal.aborted;
  }

  finish(token: LatestRequestToken): boolean {
    if (!this.isCurrent(token)) return false;
    this.#controller = null;
    return true;
  }
}
