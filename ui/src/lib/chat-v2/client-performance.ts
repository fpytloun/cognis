export type ClientPerformanceMetric = 'cached_restore_ms' | 'timeline_fresh_ms';

export interface ClientPerformanceTiming {
  cachedRestore(): void;
  timelineFresh(isCurrent: boolean): void;
}

export function createClientPerformanceTiming(
  startedAt: number,
  now: () => number,
  emit: (metric: ClientPerformanceMetric, durationMs: number) => void
): ClientPerformanceTiming {
  return {
    cachedRestore(): void {
      emit('cached_restore_ms', Math.max(0, now() - startedAt));
    },
    timelineFresh(isCurrent: boolean): void {
      if (isCurrent) emit('timeline_fresh_ms', Math.max(0, now() - startedAt));
    }
  };
}
