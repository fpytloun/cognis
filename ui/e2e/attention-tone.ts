import { expect, type Locator } from '@playwright/test';

export async function expectAttentionTone(
  locator: Locator, tone: 'accent' | 'danger', kind: 'count' | 'dot' = 'count',
): Promise<void> {
  await expect.poll(() => locator.evaluate((node, { tone, kind }) => {
    const css = getComputedStyle(node);
    const actual = css.getPropertyValue(kind === 'dot' ? 'background-color' : 'color').match(/[\d.]+/g);
    const token = kind === 'dot' ? `--color-${tone}` : `--text-${tone}`;
    const expected = css.getPropertyValue(token).match(/[\d.]+/g);
    return Boolean(actual && expected && expected.length >= 3 &&
      actual.length === expected.length && actual.every((value, index) => Number(value) === Number(expected[index])));
  }, { tone, kind })).toBe(true);
}
