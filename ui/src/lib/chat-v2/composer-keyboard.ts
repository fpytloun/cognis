export interface ComposerKeyboardOptions {
  enterToSend: boolean;
  softwareKeyboardOpen: boolean;
}

export function shouldSendComposerMessage(
  event: Pick<KeyboardEvent, 'ctrlKey' | 'isComposing' | 'key' | 'metaKey' | 'shiftKey'>,
  options: ComposerKeyboardOptions,
): boolean {
  if (event.isComposing || event.key !== 'Enter' || event.shiftKey) {
    return false;
  }
  if (options.softwareKeyboardOpen) {
    return false;
  }
  return event.metaKey || event.ctrlKey || options.enterToSend;
}
