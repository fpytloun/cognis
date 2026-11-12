import { readFileSync } from 'node:fs';
import postcss from 'postcss';
import { describe, expect, it } from 'vitest';

function touchRules(path: string) {
  const source = readFileSync(path, 'utf8');
  const style = source.match(/<style[^>]*>([\s\S]*?)<\/style>/)?.[1];
  if (!style) throw new Error(`Missing styles in ${path}`);
  const rules: { selector: string; declarations: Record<string, string> }[] = [];
  postcss.parse(style).walkAtRules('media', (media) => {
    if (media.params !== '(max-width: 767px), (pointer: coarse)') return;
    media.walkRules((rule) => {
      const declarations: Record<string, string> = {};
      rule.walkDecls((declaration) => { declarations[declaration.prop] = declaration.value; });
      rules.push({ selector: rule.selector, declarations });
    });
  });
  return rules;
}

describe('mobile and coarse-pointer usage hit areas', () => {
  it('covers every reported Usage control at its feature boundary with CSS-pixel minimums', () => {
    const rules = touchRules('src/lib/usage/UsageWorkspace.svelte');
    const controls = rules.find((rule) => rule.selector.includes(':global(button)'))!;
    for (const selector of [':global(button)', ':global(select)', ':global(summary)']) {
      expect(controls.selector).toContain(selector);
    }
    expect(controls.declarations['min-height']).toBe('44px');
    expect(controls.declarations['min-width']).toBe('44px');
    expect(controls.declarations['box-sizing']).toBe('border-box');
    expect(rules.find((rule) => rule.selector === '.pending')?.declarations['min-height']).toBe('44px');
  });

  it('includes the inspector radio labels and refresh action without enlarging desktop rules', () => {
    const rules = touchRules('src/lib/components/inspector/ConversationUsagePanel.svelte');
    expect(rules.find((rule) => rule.selector === '.segmented label, button')?.declarations['min-height']).toBe('44px');
    expect(rules.find((rule) => rule.selector === 'button')?.declarations['min-width']).toBe('44px');
  });

  it('keeps the real mobile navigation trigger at least 44 CSS pixels independent of rem scaling', () => {
    const source = readFileSync('src/routes/(app)/+layout.svelte', 'utf8');
    const trigger = source.match(/<Button aria-label="Open navigation"[^>]+>/)?.[0];
    expect(trigger).toContain('min-h-[44px]');
    expect(trigger).toContain('min-w-[44px]');
    expect(trigger).toContain('lg:hidden');
  });
});
