import { readFileSync, readdirSync } from 'node:fs';
import { join } from 'node:path';
import postcss from 'postcss';
import { describe, expect, it } from 'vitest';

function productionSources(directory: string): string[] {
  return readdirSync(directory, { withFileTypes: true }).flatMap((entry) => {
    const path = join(directory, entry.name);
    if (entry.isDirectory()) return productionSources(path);
    if (entry.name.endsWith('.test.ts')) return [];
    return /\.(?:css|html|svelte|ts)$/.test(entry.name) ? [path] : [];
  });
}

describe('Apple safe-area contract', () => {
  it('keeps bottom safe-area handling centralized in the shared app variable', () => {
    const offenders = productionSources('src').filter((path) => (
      path !== 'src/app.css'
      &&
      readFileSync(path, 'utf8').includes('safe-area-inset-bottom')
    ));

    expect(offenders).toEqual([]);
  });

  it('protects the top status area at the shared chat route frame', () => {
    const layout = readFileSync('src/routes/(app)/+layout.svelte', 'utf8');
    const styles = readFileSync('src/app.css', 'utf8');

    expect(layout).toContain('app-chat-shell-safe');
    expect(layout).toContain('app-chat-mobile-safe-top');
    expect(styles).toContain('padding-top: max(0.75rem, var(--app-safe-area-top))');
  });

  it('anchors closed app frames to fixed viewport edges instead of viewport-unit height', () => {
    const layout = readFileSync('src/routes/(app)/+layout.svelte', 'utf8');
    const dialog = readFileSync('src/lib/components/ui/BlockingDialog.svelte', 'utf8');
    const styles = readFileSync('src/app.css', 'utf8');

    expect(layout).toContain('app-shell-viewport app-viewport-frame fixed inset-x-0');
    expect(layout).not.toContain('h-[var(--app-viewport-height,100dvh)]');
    expect(dialog).toContain('app-viewport-frame fixed inset-x-0');
    expect(styles).toContain(".app-viewport-frame {\n  top: var(--app-viewport-offset-top, 0px);\n  bottom: 0;");
    expect(styles).not.toContain(":root[data-keyboard='open'] .app-viewport-frame");
  });

  it('reserves a bounded non-scrolling shell slot before app content for connectivity notices', () => {
    const layout = readFileSync('src/routes/(app)/+layout.svelte', 'utf8');
    const banner = readFileSync('src/lib/components/ConnectionBanner.svelte', 'utf8');
    const frame = layout.indexOf('app-shell-viewport app-viewport-frame fixed inset-x-0');
    const slot = layout.indexOf('data-testid="connection-notice-slot"', frame);
    const notice = layout.indexOf('<ConnectionBanner />', slot);
    const content = layout.indexOf('flex min-h-0 flex-1 w-full', notice);
    expect(frame).toBeGreaterThanOrEqual(0);
    expect(slot).toBeGreaterThan(frame);
    expect(notice).toBeGreaterThan(slot);
    expect(content).toBeGreaterThan(notice);
    expect(layout.slice(frame, slot)).toContain('max-h-[50%] shrink-0 overflow-y-auto');
    expect(banner).toContain('var(--app-safe-area-top, 0px)');
    expect(banner).not.toContain('pointer-events-none fixed inset-x-0');
  });

  it('uses the iOS-managed standalone viewport without translucent or cover offsets', () => {
    const appHtml = readFileSync('src/app.html', 'utf8');

    expect(appHtml).not.toContain('apple-mobile-web-app-status-bar-style');
    expect(appHtml).not.toContain('viewport-fit=cover');
    expect(appHtml).toContain('height=device-height');
    expect(readFileSync('src/app.css', 'utf8')).toContain('height: 100vh;');
  });

  it('uses one ordinary bottom comfort inset for application chrome', () => {
    const styles = readFileSync('src/app.css', 'utf8');
    const tabBar = readFileSync('src/lib/components/BottomTabBar.svelte', 'utf8');
    const chat = readFileSync('src/routes/(app)/chat/[conversationId]/+page.svelte', 'utf8');
    const compactChat = readFileSync('src/lib/components/chat-v2/CompactConversationChat.svelte', 'utf8');
    const taskChat = readFileSync('src/lib/components/task-cockpit/TaskControlChat.svelte', 'utf8');
    const viewport = readFileSync('src/lib/stores/viewport.ts', 'utf8');

    expect(styles).toContain('--app-bottom-comfort-inset: 0.5rem');
    expect(styles).toContain('@media (display-mode: standalone), (display-mode: fullscreen)');
    expect(styles).toContain('env(safe-area-inset-bottom, 0px)');
    expect(styles).toContain('env(safe-area-max-inset-bottom, 0px)');
    expect(styles).toContain(":root[data-standalone-pwa='true']");
    expect(viewport).toContain("root.dataset.standalonePwa = standalonePwa ? 'true' : 'false'");
    expect(styles).toContain('--app-bottom-control-inset: var(--app-bottom-comfort-inset)');
    expect(styles).toContain('max(var(--app-overlay-gap), var(--app-bottom-control-inset))');
    expect(tabBar).toContain('padding-bottom: var(--app-bottom-control-inset)');
    expect(chat).toContain('padding-bottom: var(--app-bottom-control-inset)');
    expect(compactChat).toContain('padding-bottom: var(--app-bottom-control-inset)');
    expect(taskChat).toContain('<CompactConversationChat');
  });

  it('keeps managed mobile headers compact and color-matched', () => {
    const layout = readFileSync('src/routes/(app)/+layout.svelte', 'utf8');
    const styles = readFileSync('src/app.css', 'utf8');

    expect(layout).toContain('bg-chrome px-3 py-1');
    expect(layout).toContain('class="h-10 w-10 lg:hidden');
    expect(layout).not.toContain('0.625rem+env(safe-area-inset-top)');
    expect(layout).not.toContain('chat-workspace-surface');
    expect(layout).toContain('app-shell-viewport app-viewport-frame fixed inset-x-0 overflow-hidden overscroll-none bg-chrome');
    expect(styles).toContain('.chat-header-shell,\n.app-keyboard-stable-header {');
    expect(styles).toContain('background: var(--app-chrome-bg)');
    expect(styles).not.toContain('.chat-header-shell {\n  background: transparent;');
    const manifest = JSON.parse(readFileSync('static/manifest.webmanifest', 'utf8'));
    const color = manifest.theme_color.toLowerCase();
    expect(manifest.background_color.toLowerCase()).toBe(color);
    const classic = postcss.parse(readFileSync('src/lib/theme/classic.css', 'utf8'));
    const chromeColors = ['light', 'dark'].map((mode) => {
      const values: Record<string, string> = {};
      for (const darkMedia of mode === 'dark' ? [false, true] : [false]) {
        let matched = false;
        classic.walkRules((rule) => {
          if (!rule.selectors.includes(':root:not([data-theme-family])')) return;
          if (darkMedia) {
            if (rule.parent?.type !== 'atrule' || rule.parent.name !== 'media' ||
                rule.parent.params !== '(prefers-color-scheme: dark)') return;
          } else if (rule.parent?.type !== 'root') return;
          matched = true;
          rule.walkDecls((decl) => { values[decl.prop] = decl.value; });
        });
        expect(matched, `Canonical Classic OS fallback (dark media=${darkMedia})`).toBe(true);
      }
      const resolve = (value: string): string => value.replace(/var\((--[\w-]+)\)/g, (_, role) => resolve(values[role]));
      const chrome = resolve(values['--app-chrome-bg']).toLowerCase();
      expect(chrome).toMatch(/^#[0-9a-f]{6}$/);
      return chrome;
    });
    expect(color).toBe(chromeColors[0]);
    const html = readFileSync('src/app.html', 'utf8').toLowerCase();
    expect(html).toContain(`name="theme-color" content="${chromeColors[0]}"`);
    expect(html).toContain(`dark ? '${chromeColors[1]}' : '${chromeColors[0]}'`);
    expect(styles).toContain('background-color: var(--app-chrome-bg)');
    expect(styles).toContain('linear-gradient(180deg, var(--app-chrome-bg), rgb(var(--color-canvas)) 54%)');
    expect(styles).toContain('.app-chat-mobile-safe-top {\n    padding-top: 0;');
  });

  it('keeps the app shell stable and reserves keyboard overlap only in chat bodies', () => {
    const styles = readFileSync('src/app.css', 'utf8');
    const chat = readFileSync('src/routes/(app)/chat/[conversationId]/+page.svelte', 'utf8');
    const compactChat = readFileSync('src/lib/components/chat-v2/CompactConversationChat.svelte', 'utf8');
    const taskChat = readFileSync('src/lib/components/task-cockpit/TaskControlChat.svelte', 'utf8');
    const avoidance = readFileSync('src/lib/actions/keyboard-avoidance.ts', 'utf8');
    const viewport = readFileSync('src/lib/stores/viewport.ts', 'utf8');
    const layout = readFileSync('src/routes/(app)/+layout.svelte', 'utf8');
    const workspaceWindow = readFileSync('src/lib/components/dashboard/WorkspaceWindow.svelte', 'utf8');

    expect(styles).toContain(":root[data-keyboard='open'] .app-keyboard-avoiding-chat");
    expect(styles).toContain('padding-bottom: var(--app-local-keyboard-occlusion, 0px)');
    expect(avoidance).toContain("'--app-local-keyboard-occlusion'");
    expect(chat).toContain('app-keyboard-avoiding-chat relative');
    expect(chat).toContain('use:keyboardAvoidance');
    expect(compactChat).toContain('app-keyboard-avoiding-chat relative');
    expect(compactChat).toContain('use:keyboardAvoidance');
    expect(taskChat).toContain('<CompactConversationChat');
    expect(viewport).toContain('const offsetTop = keyboardOpen ? visualOffsetTop : 0');
    expect(viewport).toContain("root.style.setProperty('--app-viewport-offset-top', '0px')");
    expect(viewport).toContain("'--app-visual-viewport-offset-top'");
    expect(styles).toContain(":root[data-keyboard='open'] .app-keyboard-stable-header");
    expect(chat).toContain('app-keyboard-stable-header chat-header-shell relative z-20');
    expect(layout).toContain('app-keyboard-stable-header fixed inset-x-0');
    expect(layout).toContain('top: var(--app-shell-notice-height, 0px)');
    expect(workspaceWindow).toContain('app-keyboard-stable-header relative z-20 flex min-h-[40px]');
  });
});
