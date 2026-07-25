export type CancellationOrigin = 'user' | 'agent' | 'controller_restart';

export function cancellationOrigin(
  status: string | null | undefined,
  detail: string | null | undefined,
): CancellationOrigin | null {
  const normalizedStatus = status?.toLowerCase() ?? '';
  if (!['cancelled', 'canceled', 'failed', 'interrupted'].includes(normalizedStatus)) {
    return null;
  }

  const normalizedDetail = detail?.toLowerCase() ?? '';
  if (normalizedDetail.includes('controller restart') || normalizedDetail.includes('parent recovered')) {
    return 'controller_restart';
  }
  if (normalizedDetail.includes('cancelled by parent session')) {
    return 'agent';
  }
  if (normalizedDetail.includes('cancelled by user') || normalizedDetail.includes('stopped by user')) {
    return 'user';
  }
  return null;
}

export function cancellationOriginLabel(origin: CancellationOrigin | null): string | null {
  switch (origin) {
    case 'user':
      return 'Cancelled by user';
    case 'agent':
      return 'Cancelled by agent';
    case 'controller_restart':
      return 'Cancelled by controller restart';
    default:
      return null;
  }
}
