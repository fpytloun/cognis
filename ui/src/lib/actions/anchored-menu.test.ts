import { afterEach, describe, expect, it, vi } from 'vitest';
import { anchoredMenu } from './anchored-menu';

function setup() {
  document.body.innerHTML = `<div style="overflow:auto;height:40px">
    <details><summary>More</summary><div class="menu"><a href="/x">Link</a><button>Stay</button><button aria-haspopup="dialog">Share</button></div></details>
  </div>`;
  const details = document.querySelector('details')!;
  const menu = document.querySelector<HTMLElement>('.menu')!;
  menu.showPopover = vi.fn();
  menu.hidePopover = vi.fn();
  const action = anchoredMenu(menu, { preferredSide: 'below' });
  return { details, menu, action };
}

describe('anchoredMenu', () => {
  afterEach(() => { document.body.innerHTML = ''; });

  it('shows the open menu in the top layer so a scrolling container cannot clip it', async () => {
    const { details, menu, action } = setup();
    expect(menu.getAttribute('popover')).toBe('manual');
    details.open = true;
    details.dispatchEvent(new Event('toggle'));
    expect(menu.showPopover).toHaveBeenCalledOnce();
    action.destroy?.();
  });

  it('closes after a link or dialog trigger is chosen, but not for other controls', () => {
    const { details, menu, action } = setup();
    details.open = true;
    menu.querySelector<HTMLButtonElement>('button:not([aria-haspopup])')!.click();
    expect(details.open).toBe(true);
    menu.querySelector<HTMLButtonElement>('[aria-haspopup="dialog"]')!.click();
    expect(details.open).toBe(false);
    details.open = true;
    menu.querySelector('a')!.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true }));
    expect(details.open).toBe(false);
    action.destroy?.();
  });
});
