export function middleOverlayOwnedSessionId(
  inspectorOpen: boolean,
  mobile: boolean,
  sessionId: string,
): string | null {
  return inspectorOpen || mobile ? sessionId : null;
}

export function focusedSessionAfterMiddleClose(
  focusedSessionId: string | null,
  overlayOwnedSessionId: string | null,
): string | null {
  return overlayOwnedSessionId !== null && focusedSessionId === overlayOwnedSessionId
    ? null
    : focusedSessionId;
}
