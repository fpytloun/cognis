import { afterEach, describe, expect, it, vi } from 'vitest';
import { openProjectWindow } from './window';

afterEach(() => { delete window.__cognisNative; delete window.webkit; vi.restoreAllMocks(); });
describe('project windows', () => {
  it('uses the native identifier-only command and no browser popup', () => {
    const send = vi.fn(), open = vi.spyOn(window, 'open').mockReturnValue(null);
    const generation = '8d6feafb-2aac-4b41-ac82-42b2314563bb';
    window.__cognisNative = { version: 1, generation, server: window.location.origin };
    window.webkit = { messageHandlers: { cognis: { postMessage: send } } };
    openProjectWindow('project_window', new URL('/projects/project_window?tab=tasks', window.location.origin));
    expect(send).toHaveBeenCalledExactlyOnceWith({
      version: 1, generation, type: 'open-resource-window', payload: { kind: 'project', id: 'project_window' }
    });
    delete window.webkit;
    openProjectWindow('project_window', new URL('/projects/project_window', window.location.origin));
    expect(open).not.toHaveBeenCalled();
  });
  it('retains the browser popup route, parameters, and opener isolation', () => {
    const open = vi.spyOn(window, 'open').mockReturnValue(null);
    openProjectWindow('project_window', new URL('/projects/project_window?tab=tasks', window.location.origin));
    expect(open).toHaveBeenCalledWith(
      `${window.location.origin}/projects/project_window?tab=tasks&window=1`,
      'cognis-project-project_window', expect.stringContaining('popup=yes,noopener,noreferrer,')
    );
  });
});
