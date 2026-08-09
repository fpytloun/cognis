export type ConversationInfoTab = 'overview' | 'work' | 'session';
export type ConversationInfoMode = ConversationInfoTab | 'context';
export type ConversationInfoPresentation = 'closed' | 'pinned' | 'overlay' | 'focus';

const STORAGE_KEY = 'cognis.conversationInfo.v2';
export const INSPECTOR_MIN_WIDTH = 384;
export const INSPECTOR_DEFAULT_WIDTH = 512;
export const INSPECTOR_MAX_WIDTH = 960;

class ConversationInfoDrawerState {
  open = $state(false);
  tab = $state<ConversationInfoTab>('overview');
  contextOpen = $state(false);
  focus = $state(false);
  preferredPinned = $state(true);
  preferredWidth = $state(INSPECTOR_DEFAULT_WIDTH);
  hydrated = $state(false);

  get mode(): ConversationInfoMode {
    return this.contextOpen ? 'context' : this.tab;
  }

  set mode(mode: ConversationInfoMode) {
    if (mode === 'context') {
      this.contextOpen = true;
      this.persist();
      return;
    }
    this.tab = mode;
    this.contextOpen = false;
    this.persist();
  }

  setOpen(open: boolean): void {
    this.open = open;
    if (!open) {
      this.focus = false;
      this.contextOpen = false;
    }
    this.persist();
  }

  hydrate(): void {
    if (this.hydrated || typeof window === 'undefined') return;
    this.hydrated = true;
    try {
      const value = JSON.parse(window.localStorage.getItem(STORAGE_KEY) ?? '{}') as {
        open?: boolean;
        tab?: ConversationInfoTab | 'full';
        preferredPinned?: boolean;
        preferredWidth?: number;
      };
      this.open = value.open === true;
      this.tab = value.tab === 'work'
        ? 'work'
        : value.tab === 'session'
          ? 'session'
          : 'overview';
      this.preferredPinned = value.preferredPinned !== false;
      if (Number.isFinite(value.preferredWidth)) {
        this.preferredWidth = this.clampWidth(value.preferredWidth!);
      }
    } catch {
      // Ignore invalid preferences and keep safe defaults.
    }
  }

  persist(): void {
    if (typeof window === 'undefined') return;
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify({
      open: this.open,
      tab: this.tab,
      preferredPinned: this.preferredPinned,
      preferredWidth: this.preferredWidth,
    }));
  }

  clampWidth(width: number): number {
    return Math.round(Math.max(INSPECTOR_MIN_WIDTH, Math.min(INSPECTOR_MAX_WIDTH, width)));
  }

  setWidth(width: number, persist = true): void {
    this.preferredWidth = this.clampWidth(width);
    if (persist) this.persist();
  }

  presentation(canPin: boolean): ConversationInfoPresentation {
    if (!this.open) return 'closed';
    if (this.focus) return 'focus';
    return canPin && this.preferredPinned ? 'pinned' : 'overlay';
  }

  close(): void {
    this.open = false;
    this.focus = false;
    this.contextOpen = false;
    this.persist();
  }
}

export const conversationInfoDrawer = new ConversationInfoDrawerState();
