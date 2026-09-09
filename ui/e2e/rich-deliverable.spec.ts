import { expect, test } from '@playwright/test';

/**
 * Force the explicit light-theme override rather than relying on
 * `emulateMedia({ colorScheme: 'light' })`: rich deliverables default to
 * dark regardless of OS preference (there is no per-deliverable theme
 * toggle, and silently following `prefers-color-scheme: light` left
 * standalone pages and embedded chat stuck light with no way back to
 * dark). The `data-resolved-theme="light"` selector is kept for a future
 * app-wide theme switch, so tests that need to exercise the light palette
 * set that attribute directly, the same way that future switch will.
 */
async function forceLightTheme(page: import('@playwright/test').Page) {
  await page.evaluate(() => {
    localStorage.setItem('theme', 'light');
    document.documentElement.dataset.resolvedTheme = 'light';
  });
}

const scenarios = [
  'research-answer',
  'incident-report',
  'product-comparison',
  'metrics-dashboard',
  'implementation-plan',
  'evidence-report',
  'publication-report',
  'newsletter-digest',
  'freeform-notes',
  'id-collision-report',
  'every-block-reference',
  'capacity-dashboard',
];

async function horizontalOverflow(page: import('@playwright/test').Page) {
  return page.evaluate(() => {
    const viewportWidth = document.documentElement.clientWidth;
    const offenders = Array.from(document.querySelectorAll<HTMLElement>('body *'))
      .filter((element) => {
        const style = getComputedStyle(element);
        const rect = element.getBoundingClientRect();
        if (
          style.position === 'fixed' ||
          style.position === 'absolute' ||
          style.display === 'none' ||
          rect.width === 0 ||
          rect.height === 0
        ) return false;

        let effectiveRect = rect;
        let ancestor = element.parentElement;
        while (ancestor) {
          const ancestorStyle = getComputedStyle(ancestor);
          const ancestorRect = ancestor.getBoundingClientRect();
          const locallyScrollable =
            ['auto', 'scroll'].includes(ancestorStyle.overflowX) &&
            ancestor.scrollWidth > ancestor.clientWidth + 1;
          const clippedByAncestor =
            ['hidden', 'clip'].includes(ancestorStyle.overflowX) &&
            (effectiveRect.left < ancestorRect.left - 1 || effectiveRect.right > ancestorRect.right + 1);
          if (clippedByAncestor) return true;
          const accessibleScrollport =
            ancestorRect.left >= -1 && ancestorRect.right <= viewportWidth + 1;
          if (locallyScrollable && accessibleScrollport) effectiveRect = ancestorRect;
          ancestor = ancestor.parentElement;
        }
        if (effectiveRect.right <= viewportWidth + 1 && effectiveRect.left >= -1) return false;
        return true;
      })
      .slice(0, 12)
      .map((element) => ({
        tag: element.tagName,
        className: element.className,
        left: element.getBoundingClientRect().left,
        right: element.getBoundingClientRect().right,
      }));
    return { documentWidth: document.documentElement.scrollWidth, viewportWidth, offenders };
  });
}

async function expectNoHorizontalClipping(page: import('@playwright/test').Page) {
  const overflow = await horizontalOverflow(page);
  expect(overflow.documentWidth, JSON.stringify(overflow.offenders, null, 2)).toBeLessThanOrEqual(overflow.viewportWidth + 1);
  expect(overflow.offenders, JSON.stringify(overflow.offenders, null, 2)).toEqual([]);
}

async function pulseDisclosureContrast(panel: import('@playwright/test').Locator) {
  return panel.evaluate((panel) => {
    const parse = (value: string) => {
      const channels = value.match(/[\d.]+/g)?.map(Number) ?? [];
      return channels.slice(0, 3);
    };
    const luminance = (channels: number[]) => {
      const linear = channels.map((value) => {
        const normalized = value / 255;
        return normalized <= 0.04045 ? normalized / 12.92 : ((normalized + 0.055) / 1.055) ** 2.4;
      });
      return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2];
    };
    const background = (element: Element) => {
      let current: Element | null = element;
      while (current) {
        const color = getComputedStyle(current).backgroundColor;
        if (color !== 'rgba(0, 0, 0, 0)' && color !== 'transparent') return color;
        current = current.parentElement;
      }
      return 'rgb(255, 255, 255)';
    };
    return ['summary span', 'summary small', '.rich-disclosure-body', '.rich-disclosure-source']
      .map((selector) => panel.querySelector<HTMLElement>(selector))
      .filter((element): element is HTMLElement => Boolean(element))
      .map((element) => {
        const foreground = getComputedStyle(element).color;
        const backdrop = background(element);
        const foregroundLuminance = luminance(parse(foreground));
        const backgroundLuminance = luminance(parse(backdrop));
        const ratio = (Math.max(foregroundLuminance, backgroundLuminance) + 0.05)
          / (Math.min(foregroundLuminance, backgroundLuminance) + 0.05);
        return { text: element.textContent?.trim(), foreground, backdrop, ratio };
      });
  });
}

test.describe('rich deliverable visual fixture', () => {
  test.use({ bypassCSP: true });

  for (const width of [320, 390, 430, 1440]) {
    test(`renders the daily pulse newsroom composition at ${width}px`, async ({ page }, testInfo) => {
      await page.setViewportSize({ width, height: width === 1440 ? 1200 : 1000 });
      await page.goto('/rich-deliverable-fixture');
      if (width === 390) await forceLightTheme(page);
      await page.getByRole('tab', { name: 'Ranní Pulse v2', exact: true }).click();

      const fixture = page.getByTestId('rich-deliverable-fixture');
      const pulse = page.getByTestId('rich-deliverable');
      await expect(fixture).toHaveAttribute('data-scenario', 'daily-pulse-v2');
      await expect(pulse).toHaveAttribute('data-presentation', 'pulse');
      await expect(pulse.getByRole('heading', { level: 1 })).toHaveCount(1);
      await expect(pulse.getByTestId('rich-deliverable-toc')).toHaveCount(0);
      await expect(pulse.getByText(/Figure 1\./)).toHaveCount(0);
      const media = pulse.getByRole('img', { name: /Lovosice a České středohoří/ });
      await expect(media).toBeVisible();
      await expect(media).toHaveAttribute('width', '1600');
      await expect(media).toHaveAttribute('height', '900');
      await expect(pulse.getByRole('heading', { name: 'Dnes udělat', exact: true })).toBeVisible();
      await expect(pulse.getByRole('region', { name: 'Úterý 14. července' })).toBeVisible();
      await expect(pulse.getByLabel('Aktuální čas 08:20')).toBeVisible();
      await expect(pulse.getByText('Odeslat rozhodnutí')).toBeVisible();
      await expect(pulse.getByLabel('Todoist úkoly')).toContainText('Potvrdit prioritu');
      await expect(pulse.getByText('Před delší cestou stačí ověřit jedinou trasu.')).toBeVisible();
      await expect(pulse.getByRole('heading', { name: 'Vědět', exact: true })).toBeVisible();
      await expect(pulse.getByRole('heading', { name: 'Sledovat', exact: true })).toBeVisible();
      await expect(pulse.getByText('Dnešní kurz', { exact: true })).toBeVisible();
      await expectNoHorizontalClipping(page);
      if (width === 390) {
        const disclosure = pulse.locator('[data-rich-block-type="accordion"] > details.rich-panel').first();
        await disclosure.locator(':scope > summary').evaluate((summary) => {
          const details = summary.parentElement as HTMLDetailsElement | null;
          if (details) details.open = true;
          summary.scrollIntoView({ block: 'center' });
        });
        const contrast = await pulseDisclosureContrast(disclosure);
        expect(contrast).toHaveLength(4);
        for (const sample of contrast) {
          expect(sample.ratio, JSON.stringify(sample)).toBeGreaterThanOrEqual(4.5);
        }
      }

      const chatPath = testInfo.outputPath(`daily-pulse-chat-${width}.png`);
      await pulse.screenshot({ path: chatPath });
      await testInfo.attach(`daily-pulse-chat-${width}.png`, {
        path: chatPath,
        contentType: 'image/png',
      });
      const browserPagePath = testInfo.outputPath(`daily-pulse-browser-page-${width}.png`);
      await page.screenshot({ path: browserPagePath, fullPage: true });
      await testInfo.attach(`daily-pulse-browser-page-${width}.png`, {
        path: browserPagePath,
        contentType: 'image/png',
      });

      await pulse.getByRole('button', { name: 'Open full view' }).click();
      await expect(page.getByTestId('rich-deliverable-full-view')).toBeVisible();
      await expectNoHorizontalClipping(page);
      const fullPath = testInfo.outputPath(`daily-pulse-full-${width}.png`);
      await page.screenshot({ path: fullPath, fullPage: true });
      await testInfo.attach(`daily-pulse-full-${width}.png`, {
        path: fullPath,
        contentType: 'image/png',
      });
    });
  }

  test('renders polished real-world rich deliverable scenarios', async ({ page }, testInfo) => {
    await page.goto('/rich-deliverable-fixture');

    const fixture = page.getByTestId('rich-deliverable-fixture');
    await expect(page.getByRole('heading', { name: 'Rich Deliverables' })).toBeVisible();
    await expect(fixture).toHaveAttribute('data-scenario', 'research-answer');
    await expect(page.getByTestId('rich-deliverable')).toBeVisible();
    await expect(page.getByTestId('rich-deliverable-toc')).toHaveCount(0);
    await expect(page.getByTestId('rich-deliverable-body')).toBeVisible();
    await expect(page.locator('[data-rich-block-type="hero"]')).toBeVisible();
    await expect(page.locator('[data-rich-block-type="metric"]').first()).toBeVisible();
    await expect(page.locator('[data-rich-block-type="comparison_matrix"]').first()).toBeVisible();
    await expect(page.locator('[data-rich-block-type="source_list"]').first()).toBeVisible();
    await expect(page.getByText(/Unsupported block:/)).toHaveCount(0);

    for (const scenario of scenarios.slice(1)) {
      await page.getByRole('tab', { name: new RegExp(scenario.replace(/-/g, '.*'), 'i') }).click();
      await expect(fixture).toHaveAttribute('data-scenario', scenario);
      await expect(page.getByText(/Unsupported block:/)).toHaveCount(0);
    }

    await page.getByRole('tab', { name: /implementation.*plan/i }).click();
    await expect(page.locator('[data-rich-block-type="steps"]')).toBeVisible();
    await expect(page.locator('[data-rich-block-type="mermaid"]')).toBeVisible();
    await expect(page.locator('[data-rich-block-type="code"]')).toBeVisible();

    await page.getByRole('button', { name: 'Open full view' }).click();
    await expect(page.getByTestId('rich-deliverable-full-view')).toBeVisible();
    await page.keyboard.press('Escape');
    await expect(page.getByTestId('rich-deliverable-full-view')).toHaveCount(0);

    await expect(page.getByRole('button', { name: 'Raw/debug' })).toHaveCount(0);
    await expect(page.getByTestId('rich-deliverable-raw')).toHaveCount(0);

    const screenshot = await page.screenshot({ fullPage: true });
    await testInfo.attach('rich-deliverable-fixture.png', {
      body: screenshot,
      contentType: 'image/png',
    });
  });

  test('renders a readable document header for payloads that do not lead with a hero block', async ({ page }) => {
    // Regression test: the document-level title/eyebrow/subtitle header
    // (`.rich-toolbar`) is a CSS sibling of `.rich-block-list`, not a
    // descendant, and only renders when the first authored block is not
    // `hero`. It previously had no working design tokens at all in that
    // path (tokens were scoped to `.rich-block-list`), silently falling
    // back to an inherited/undefined color regardless of theme. Almost
    // every other fixture leads with `hero`, which suppresses this header
    // entirely, so this path was untested until the freeform-notes
    // archetype fixture. `.rich-toolbar` uses a gradient `background` (not
    // a solid `background-color`), so a composited-backdrop contrast check
    // is not reliable here; instead assert the resolved token colors
    // directly match the light-theme palette and differ from the
    // dark-theme palette, proving the CSS custom properties actually
    // reach this element rather than resolving to an unrelated ambient
    // color that happened to look plausible in one theme.
    await page.goto('/rich-deliverable-fixture');
    await page.getByRole('tab', { name: /freeform.*notes/i }).click();
    await forceLightTheme(page);
    await expect(page.getByTestId('rich-deliverable-fixture')).toHaveAttribute('data-scenario', 'freeform-notes');

    const toolbar = page.getByTestId('rich-deliverable-toolbar');
    await expect(toolbar).toBeVisible();
    await expect(toolbar.getByRole('heading', { level: 1 })).toHaveText('Freeform working notes');
    await expect(page.locator('[data-rich-block-type="hero"]')).toHaveCount(0);

    const colors = await page.evaluate(() => ({
      h1: getComputedStyle(document.querySelector('.rich-toolbar h1')!).color,
      eyebrow: getComputedStyle(document.querySelector('.rich-eyebrow')!).color,
      subtitle: getComputedStyle(document.querySelector('.rich-toolbar p')!).color,
    }));
    // Light-theme --rich-text / --rich-text-secondary / --rich-accent-soft.
    expect(colors.h1, JSON.stringify(colors)).toBe('rgb(23, 32, 51)');
    expect(colors.subtitle, JSON.stringify(colors)).toBe('rgb(51, 65, 85)');
    expect(colors.eyebrow, JSON.stringify(colors)).toBe('rgb(2, 132, 199)');
  });

  test('renders readable colors in the full-view modal, which is portaled outside .rich-deliverable', async ({ page }) => {
    // Regression test: `.rich-full` (the full-view modal) is moved to
    // `document.body` via `use:portal` specifically to escape this
    // component's isolated stacking context, so it is NOT a DOM descendant
    // of `.rich-deliverable` even though it contains its own
    // `.rich-block-list`. A design-token refactor that scoped tokens to
    // only `.rich-deliverable` (correctly fixing the `.rich-toolbar`
    // sibling case above) silently broke this instead, with the exact same
    // symptom: `--rich-text` resolved to an empty string and text fell back
    // to an unrelated ambient color. Tokens must be defined on both
    // `.rich-deliverable` and `.rich-full`.
    await page.goto('/rich-deliverable-fixture');
    await page.getByRole('tab', { name: 'Weekly metrics dashboard', exact: true }).click();
    await page.getByRole('button', { name: 'Open full view' }).click();
    await forceLightTheme(page);
    const dialog = page.getByTestId('rich-deliverable-full-view');
    await expect(dialog).toBeVisible();

    const colors = await dialog.evaluate((full) => ({
      richText: getComputedStyle(full).getPropertyValue('--rich-text').trim(),
      h1: getComputedStyle(full.querySelector('h1, h2')!).color,
      blockList: getComputedStyle(full.querySelector('.rich-block-list')!).color,
    }));
    expect(colors.richText, JSON.stringify(colors)).not.toBe('');
    expect(colors.h1, JSON.stringify(colors)).toBe('rgb(23, 32, 51)');
    expect(colors.blockList, JSON.stringify(colors)).toBe('rgb(51, 65, 85)');
  });

  test('gives every registered block type dedicated visual treatment (design-system polish pass)', async ({ page }) => {
    await page.goto('/rich-deliverable-fixture');
    await page.getByRole('tab', { name: 'Every block type reference', exact: true }).click();
    await expect(page.getByTestId('rich-deliverable-fixture')).toHaveAttribute('data-scenario', 'every-block-reference');
    await expect(page.getByText(/Unsupported block:/)).toHaveCount(0);
    const rich = page.getByTestId('rich-deliverable');

    // gallery: tighter photo-grid sizing, not the plain card-sized grid.
    const gallery = rich.locator('[data-rich-block-type="gallery"]');
    await expect(gallery).toBeVisible();
    const galleryColumnWidth = await gallery.evaluate((el) => getComputedStyle(el).gridTemplateColumns);
    expect(galleryColumnWidth, galleryColumnWidth).not.toBe('none');

    // steps: a numbered-circle marker via the rich-steps modifier class,
    // not a plain timeline label.
    const stepsMarker = rich.locator('[data-rich-block-type="steps"] li > span').first();
    await expect(stepsMarker).toHaveText('1');
    const stepsMarkerRadius = await stepsMarker.evaluate((el) => getComputedStyle(el).borderRadius);
    expect(stepsMarkerRadius, stepsMarkerRadius).toBe('999px');

    // action: a trailing arrow on the linked heading for clearer CTA identity.
    const actionLink = rich.locator('[data-rich-block-type="action"] h4 a');
    await expect(actionLink).toBeVisible();
    const actionArrow = await actionLink.evaluate((el) => getComputedStyle(el, '::after').content);
    expect(actionArrow).toContain('\u2192');

    // code: the authored language field is surfaced (previously silently dropped).
    await expect(rich.locator('[data-rich-block-type="code"] .rich-code-language')).toHaveText('python');
  });

  for (const width of [320, 375, 390, 430]) {
    test(`keeps rich deliverables usable without clipping at ${width}px`, async ({ page }, testInfo) => {
      await page.setViewportSize({ width, height: 1200 });
      await page.goto('/rich-deliverable-fixture');

      const fixture = page.getByTestId('rich-deliverable-fixture');
      await expect(fixture).toHaveAttribute('data-scenario', 'research-answer');
      await expect(page.getByTestId('rich-deliverable')).toBeVisible();
      await expect(page.locator('[data-rich-block-type="comparison_matrix"] td[data-label="Dimension"]').first()).toBeVisible();
      await expect(page.getByText(/Unsupported block:/)).toHaveCount(0);
      await expectNoHorizontalClipping(page);

      await page.getByRole('tab', { name: /implementation.*plan/i }).click();
      await expect(page.locator('[data-rich-block-type="code"]')).toBeVisible();
      await expectNoHorizontalClipping(page);

      await page.getByRole('button', { name: 'Open full view' }).click();
      const dialog = page.getByTestId('rich-deliverable-full-view');
      await expect(dialog).toBeVisible();
      await expect(page.getByRole('button', { name: 'Close' })).toBeFocused();
      await expectNoHorizontalClipping(page);

      await page.keyboard.press('Shift+Tab');
      await expect(dialog.locator(':focus')).toHaveCount(1);
      await expect(page.getByRole('button', { name: 'Close' })).not.toBeFocused();
      await page.keyboard.press('Tab');
      await expect(page.getByRole('button', { name: 'Close' })).toBeFocused();

      await page.keyboard.press('Escape');
      await expect(dialog).toHaveCount(0);
      await expect(page.getByRole('button', { name: 'Open full view' })).toBeFocused();

      const screenshot = await page.screenshot({ fullPage: true });
      await testInfo.attach(`rich-deliverable-mobile-${width}.png`, {
        body: screenshot,
        contentType: 'image/png',
      });
    });
  }

  test('detects inaccessible clipping hidden by a bounded ancestor', async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 800 });
    await page.goto('/rich-deliverable-fixture');
    await page.evaluate(() => {
      const clip = document.createElement('div');
      clip.dataset.testid = 'inaccessible-clipping-regression';
      clip.style.cssText = 'position: relative; width: 100%; overflow: hidden';
      const child = document.createElement('div');
      child.className = 'inaccessible-clipping-child';
      child.style.cssText = 'width: 200px; height: 1px; transform: translateX(500px)';
      clip.append(child);
      document.body.append(clip);
    });
    await expect(page.getByTestId('inaccessible-clipping-regression')).toHaveCount(1);

    const overflow = await horizontalOverflow(page);
    expect(overflow.documentWidth).toBeLessThanOrEqual(overflow.viewportWidth + 1);
    expect(
      overflow.offenders.some((item) => item.right > overflow.viewportWidth + 1),
      JSON.stringify(overflow, null, 2),
    ).toBe(true);
  });

  test('detects clipping inside a narrower hidden ancestor while the child fits the viewport', async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 800 });
    await page.goto('/rich-deliverable-fixture');
    await page.evaluate(() => {
      const clip = document.createElement('div');
      clip.dataset.testid = 'narrow-hidden-ancestor-regression';
      clip.style.cssText = 'width: 100px; overflow: hidden';
      const child = document.createElement('div');
      child.className = 'narrow-hidden-child';
      child.style.cssText = 'width: 200px; height: 1px';
      clip.append(child);
      document.body.append(clip);
    });

    const overflow = await horizontalOverflow(page);
    expect(overflow.documentWidth).toBeLessThanOrEqual(overflow.viewportWidth + 1);
    expect(
      overflow.offenders.some((item) => item.className === 'narrow-hidden-child'),
      JSON.stringify(overflow, null, 2),
    ).toBe(true);
  });

  test('keeps polished rich deliverable interactions and full-view layering intact', async ({ page }, testInfo) => {
    await page.setViewportSize({ width: 1440, height: 1800 });
    await page.goto('/rich-deliverable-fixture');
    await expectNoHorizontalClipping(page);

    await page.getByRole('tab', { name: /weekly.*metrics.*dashboard/i }).click();
    await expect(page.getByTestId('rich-deliverable-fixture')).toHaveAttribute('data-scenario', 'metrics-dashboard');

    const markdownOnly = page.getByRole('button', { name: 'Markdown-only' });
    await markdownOnly.evaluate((element) => element.scrollIntoView({ block: 'center' }));
    await expect(markdownOnly).toHaveAttribute('aria-pressed', 'true');
    await markdownOnly.click();
    await expect(markdownOnly).toHaveAttribute('aria-pressed', 'false');

    const volumeHeader = page.getByRole('button', { name: /sort by volume/i });
    await volumeHeader.focus();
    await expect(volumeHeader).toBeFocused();
    await volumeHeader.press('Enter');
    await expect(page.locator('[data-rich-block-type="table"] tbody tr').first()).toContainText('Incident summaries');

    const headerButtonStyles = await volumeHeader.evaluate((element) => {
      const styles = getComputedStyle(element);
      return {
        borderTopWidth: styles.borderTopWidth,
        backgroundColor: styles.backgroundColor,
        borderRadius: styles.borderRadius,
      };
    });
    expect(headerButtonStyles.borderTopWidth).toBe('0px');
    expect(headerButtonStyles.borderRadius).not.toBe('999px');

    await page.getByRole('tab', { name: /implementation.*plan/i }).click();
    await expect(page.locator('[data-rich-block-type="mermaid"]')).toBeVisible();
    await expect(page.locator('.rich-mermaid svg')).toBeVisible();

    await page.getByRole('button', { name: 'Open full view' }).click();
    const dialog = page.getByTestId('rich-deliverable-full-view');
    await expect(dialog).toBeVisible();
    const zIndex = await dialog.evaluate((element) => Number(getComputedStyle(element).zIndex));
    expect(zIndex).toBeGreaterThan(1000);
    await expect(page.locator('.rich-mermaid svg').last()).toBeVisible();

    const screenshot = await page.screenshot({ fullPage: true });
    await testInfo.attach('rich-deliverable-polish.png', {
      body: screenshot,
      contentType: 'image/png',
    });
  });

  test('renders canonical chart, mermaid, and divider children once', async ({ page }) => {
    await page.goto('/rich-deliverable-fixture');
    await page.getByRole('tab', { name: /implementation.*plan/i }).click();
    await expect(page.getByTestId('rich-deliverable-fixture')).toHaveAttribute(
      'data-scenario',
      'implementation-plan',
    );

    for (const marker of [
      'Chart child content is visible exactly once.',
      'Mermaid child content is visible exactly once.',
      'Divider child content follows the divider exactly once.',
    ]) {
      await expect(page.getByText(marker, { exact: true })).toHaveCount(1);
      await expect(page.getByText(marker, { exact: true })).toBeVisible();
    }

    const divider = page.locator('[data-rich-block-type="divider"]');
    await expect(divider.locator(':scope > hr')).toHaveCount(1);
    await expect(divider.getByText('Divider child content follows the divider exactly once.')).toBeVisible();
  });

  for (const width of [320, 390, 430, 1440]) {
    test(`renders generic visual cards safely at ${width}px`, async ({ page }, testInfo) => {
      await page.emulateMedia({ reducedMotion: 'reduce' });
      await page.setViewportSize({ width, height: 1000 });
      await page.goto('/rich-deliverable-fixture');
      if (width !== 1440) await forceLightTheme(page);
      await page.getByRole('tab', { name: 'Generic visual system reference', exact: true }).click();

      const document = page.getByTestId('rich-deliverable');
      await expect(document).toHaveAttribute('data-presentation', 'default');
      await expect(document.locator('[data-rich-card-variant="feature"]')).toBeVisible();
      await expect(document.getByRole('img', { name: 'Abstract blue editorial illustration', exact: true })).toBeVisible();
      await expect(document.getByRole('link', { name: 'Renderer health is stable' })).toHaveAttribute('href', 'https://example.org/fixture/health');
      await expect(document.locator('[data-rich-icon="activity"]')).toBeVisible();
      await expect(document.locator('[data-rich-icon="unknown_icon_name"]')).toHaveCount(0);
      await expect(document.getByText('The summary stays visible while the contextual notes and evidence remain opt-in.')).toBeVisible();
      await expect(document.getByText('Expanded editorial context avoids repeating the section title and can include supporting citations.')).toBeHidden();
      const disclosure = document.locator('summary').filter({ hasText: 'What changed' });
      await disclosure.evaluate((element) => (element as HTMLElement).click());
      await expect(document.getByText('Expanded editorial context avoids repeating the section title and can include supporting citations.')).toBeVisible();
      await expect(document.getByTestId('rich-chart-baseline')).toBeVisible();
      await expectNoHorizontalClipping(page);

      const screenshot = testInfo.outputPath(`rich-visual-system-${width}.png`);
      await page.screenshot({ path: screenshot, fullPage: true });
      await testInfo.attach(`rich-visual-system-${width}.png`, { path: screenshot, contentType: 'image/png' });
    });
  }

  test('supports publication TOC, citations, previews, captions, and keyboard navigation', async ({ page }, testInfo) => {
    await page.setViewportSize({ width: 1280, height: 1000 });
    await page.goto('/rich-deliverable-fixture');
    await page.getByRole('tab', { name: /publication-grade technical report/i }).click();
    await expect(page.getByTestId('rich-deliverable-fixture')).toHaveAttribute('data-scenario', 'publication-report');
    const report = page.getByTestId('rich-deliverable');
    const namespace = await report.getAttribute('data-rich-instance');

    const toc = page.getByTestId('rich-deliverable-toc');
    const tocTrigger = report.getByRole('button', { name: 'Open table of contents' });
    if (await tocTrigger.isVisible()) await tocTrigger.click();
    await expect(toc).toBeVisible();
    await expect(toc.locator('small, [class*="badge"], [class*="card"]')).toHaveCount(0);
    const evaluationLink = toc.getByRole('button', { name: 'Evaluation' }).first();
    await evaluationLink.focus();
    await expect(evaluationLink).toBeFocused();
    await evaluationLink.press('Enter');
    await expect(page.locator(`#${namespace}-evaluation`)).toBeFocused();

    const citation = page.getByRole('button', { name: /Citation 1:/ }).first();
    await citation.focus();
    await citation.press('Enter');
    await expect(page.getByRole('dialog', { name: /Source WeasyPrint Features/i })).toBeVisible();
    await page.keyboard.press('Escape');

    await expect(page.getByText('Figure 1.')).toBeVisible();
    await expect(page.getByText('Table 1.')).toBeVisible();
    const figureImage = page.getByRole('img', { name: 'Three-stage renderer pipeline' });
    await expect(figureImage).toBeVisible();
    expect(await figureImage.evaluate((image) => ({
      naturalWidth: (image as HTMLImageElement).naturalWidth,
      clientWidth: (image as HTMLImageElement).clientWidth,
      parentWidth: image.parentElement?.clientWidth ?? 0,
    }))).toEqual(expect.objectContaining({ naturalWidth: expect.any(Number) }));
    expect(await figureImage.evaluate((image) => (image as HTMLImageElement).naturalWidth)).toBeGreaterThan(0);
    expect(await figureImage.evaluate((image) => image.clientWidth <= (image.parentElement?.clientWidth ?? 0))).toBe(true);
    const figureCaption = page.locator('figure figcaption').filter({ hasText: 'Renderer-neutral blocks' });
    await expect(figureCaption).toBeVisible();
    await expect(figureCaption).toContainText('Figure 1.');
    await expect(toc.getByRole('button', { name: 'Operational notes' })).toHaveCount(0);
    await expect(toc.getByRole('button', { name: 'Reviewer notes' })).toHaveCount(0);
    await toc.getByRole('button', { name: 'Supplemental views' }).evaluate((element) => (element as HTMLElement).click());
    await expect(page.locator(`#${namespace}-supplemental-views`)).toBeFocused();
    await toc.getByRole('button', { name: 'Closing summary' }).evaluate((element) => (element as HTMLElement).click());
    await expect(page.locator(`#${namespace}-closing-summary`)).toBeFocused();
    await expect(page.locator(`#${namespace}-closing-summary`)).toHaveCount(1);
    const preview = page.locator('summary').filter({ hasText: /^Source preview$/ }).first();
    await preview.evaluate((element) => (element as HTMLElement).click());
    await expect(preview.locator('..').getByText(/WeasyPrint supports PDF bookmarks/)).toBeVisible();
    await expectNoHorizontalClipping(page);

    await testInfo.attach('rich-deliverable-publication.png', {
      body: await page.screenshot({ fullPage: true }),
      contentType: 'image/png',
    });
  });

  test('uses one accessible TOC drawer beside actions on narrow mobile', async ({ page }, testInfo) => {
    await page.setViewportSize({ width: 390, height: 760 });
    await page.goto('/rich-deliverable-fixture');
    await page.getByRole('tab', { name: /publication-grade technical report/i }).click();
    const report = page.getByTestId('rich-deliverable');
    // The drawer is portaled to document.body (to escape .rich-deliverable's
    // isolated stacking context), so it is no longer a DOM descendant of
    // `report` -- assert against `page`, not `report`, for drawer-scoped
    // locators (mirrors how the full-view modal's own portaled TOC is
    // asserted against `page`/`dialog` elsewhere in this file).
    await expect(page.getByTestId('rich-deliverable-toc')).toHaveCount(1);
    await expect(page.locator('nav[aria-label="Table of contents"]')).toHaveCount(1);
    const actions = report.getByRole('navigation', { name: 'Document actions' });
    const trigger = actions.getByRole('button', { name: 'Open table of contents' });
    await expect(trigger).toBeVisible();
    await expect(actions).toBeVisible();
    await expect(report.getByRole('heading', { level: 1 })).toHaveCount(1);
    await trigger.focus();
    await trigger.click();
    const toc = page.locator('nav[aria-label="Table of contents"]');
    await expect(toc).toHaveAttribute('role', 'dialog');
    await expect(toc.getByRole('button', { name: 'Close table of contents' })).toBeFocused();
    await page.keyboard.press('Escape');
    await expect(trigger).toBeFocused();
    await trigger.click();
    await toc.getByRole('button', { name: 'Evaluation' }).click();
    await expect(toc).not.toHaveAttribute('role', 'dialog');
    expect(await page.evaluate(() => document.documentElement.scrollWidth))
      .toBeLessThanOrEqual(390);

    const path = testInfo.outputPath('rich-deliverable-publication-mobile-390.png');
    await page.screenshot({ path, fullPage: true });
    await testInfo.attach('rich-deliverable-publication-mobile-390.png', {
      path,
      contentType: 'image/png',
    });
  });

  test('full-view TOC preserves SvelteKit history across route navigation and Back', async ({ page }) => {
    await page.setViewportSize({ width: 1600, height: 1000 });
    await page.goto('/rich-deliverable-fixture');
    await page.getByRole('tab', { name: /publication-grade technical report/i }).click();
    await page.getByRole('button', { name: 'Open full view' }).click();
    const dialog = page.getByRole('dialog');
    const stateBefore = await page.evaluate(() => history.state);

    const tocTrigger = dialog.getByRole('button', { name: 'Open table of contents' });
    if (await tocTrigger.isVisible()) await tocTrigger.click();
    await dialog.locator('nav[aria-label="Table of contents"]')
      .getByRole('button', { name: 'Evaluation' })
      .click();
    const reportUrl = page.url();
    const stateAfterToc = await page.evaluate(() => history.state);
    expect(stateAfterToc).toEqual(stateBefore);
    expect(new URL(reportUrl).hash).toContain('evaluation');

    await page.goto('/rich-deliverable-multi-fixture');
    await expect(page.getByTestId('rich-deliverable-multi-fixture')).toBeVisible();
    await page.goBack();

    await expect(page).toHaveURL(reportUrl);
    await expect(page.getByTestId('rich-deliverable-fixture')).toBeVisible();
    expect(await page.evaluate(() => history.state)).toEqual(stateBefore);
  });

  test('keeps full-view IDs unique and navigates current and legacy fragments in the visible copy', async ({ page }) => {
    await page.setViewportSize({ width: 1600, height: 900 });
    await page.goto('/rich-deliverable-fixture');
    await page.getByRole('tab', { name: /publication-grade technical report/i }).click();
    await page.getByRole('button', { name: 'Open full view' }).click();

    const fullView = page.getByTestId('rich-deliverable-full-view');
    const namespace = await page.getByTestId('rich-deliverable').getAttribute('data-rich-instance');
    await expect(fullView).toBeVisible();
    const duplicateIds = await page.locator('[id]').evaluateAll((elements) => {
      const ids = elements.map((element) => element.id).filter(Boolean);
      return ids.filter((id, index) => ids.indexOf(id) !== index);
    });
    expect(duplicateIds).toEqual([]);
    await expect(page.locator(`#${namespace}-evaluation`)).toHaveCount(1);
    await expect(page.locator(`#${namespace}-rich-section-4`)).toHaveCount(1);

    const tocTrigger = fullView.getByRole('button', { name: 'Open table of contents' });
    if (await tocTrigger.isVisible()) await tocTrigger.click();
    await fullView.getByRole('button', { name: 'Evaluation' }).first().click();
    await expect(page.locator(`#${namespace}-evaluation`)).toBeFocused();

    await page.evaluate(() => {
      const root = document.querySelector<HTMLElement>('[data-rich-instance]');
      window.location.hash = `#${root?.dataset.richInstance}-rich-section-4`;
    });
    await expect(page.locator(`#${namespace}-rich-section-4`)).toBeAttached();
    await expect(page.locator(`#${namespace}-rich-section-4`)).toBeInViewport();
  });

  test('keeps adversarial generated namespaces unique and titled Markdown semantic', async ({ page }) => {
    await page.setViewportSize({ width: 1280, height: 900 });
    await page.goto('/rich-deliverable-fixture');
    await page.getByRole('tab', { name: /id collision report/i }).click();
    const namespace = await page.getByTestId('rich-deliverable').getAttribute('data-rich-instance');
    const mermaidSvg = page.locator('.rich-mermaid svg');
    await expect(mermaidSvg).toHaveCount(1);

    await expect.poll(() => page.locator('[id]').evaluateAll((elements) => {
      const ids = elements.map((element) => element.id).filter(Boolean);
      return ids.filter((id, index) => ids.indexOf(id) !== index);
    })).toEqual([]);
    for (const id of [
      'section-rich-section-0',
      'section-reference-1',
      'section-references-heading',
      'section-cite-1-1',
      'section-toc',
      'section-figure-1',
      'section-table-1',
      'section-mermaid-0',
      'duplicate',
      'duplicate-2',
    ]) await expect(page.locator(`#${namespace}-${id}`)).toHaveCount(1);
    await expect(mermaidSvg).toHaveAttribute('id', `${namespace}-mermaid-0`);
    await expect(page.locator(`#${namespace}-section-mermaid-0`)).toHaveCount(1);

    const toc = page.getByTestId('rich-deliverable-toc');
    const tocTrigger = page.getByRole('button', { name: 'Open table of contents' });
    if (await tocTrigger.isVisible()) await tocTrigger.click();
    await toc.getByRole('button', { name: /Summary$/ }).first().click();
    await expect(page.locator(`h2#${namespace}-summary`)).toBeFocused();
    await expect(page.locator(`h2#${namespace}-summary`)).toHaveText('Summary');
    await toc.getByRole('button', { name: /Content heading$/ }).first().evaluate(
      (element) => (element as HTMLElement).click()
    );
    await expect(page.locator(`h3#${namespace}-content-heading`)).toBeFocused();
    await expect(page.locator(`h4#${namespace}-detail-heading`)).toHaveText('Detail heading');

    const citation = page.getByRole('button', { name: /Citation 1: Collision source/ });
    const controlledId = await citation.getAttribute('aria-controls');
    expect(controlledId).toBeTruthy();
    await citation.evaluate((element) => (element as HTMLElement).click());
    await expect(page.locator(`#${controlledId}`)).toHaveCount(1);
  });
});

test.describe('multiple rich deliverables', () => {
  test.use({ bypassCSP: true });

  for (const viewport of [
    { name: 'desktop', width: 1600, height: 900 },
    { name: 'mobile-390', width: 390, height: 844 },
  ]) {
    test(`namespaces identical reports at ${viewport.name}`, async ({ page }) => {
      await page.setViewportSize(viewport);
      await page.goto('/rich-deliverable-multi-fixture');
      const reports = page.getByTestId('rich-deliverable');
      await expect(reports).toHaveCount(2);
      await expect(page.getByTestId('rich-deliverable-toc')).toHaveCount(2);
      // The hamburger trigger is no longer restricted to surface="standalone"
      // (that restriction was the bug: embedded chat deliverables had no way
      // to open their TOC at all). It is now purely width-driven: hidden at
      // >=1280px (sticky sidebar has no need for a trigger), shown below
      // that -- one per embedded report -- regardless of surface.
      await expect(page.getByRole('button', { name: 'Open table of contents' })).toHaveCount(2);
      const ids = await page.locator('[id]').evaluateAll((elements) =>
        elements.map((element) => element.id).filter(Boolean)
      );
      expect(new Set(ids).size).toBe(ids.length);

      for (let index = 0; index < 2; index += 1) {
        const report = reports.nth(index);
        const namespace = await report.getAttribute('data-rich-instance');
        expect(namespace).toBeTruthy();
        const citation = report.getByRole('button', { name: /Citation 1: Shared source/ });
        const controlledId = await citation.getAttribute('aria-controls');
        expect(controlledId).toContain(namespace);
        await citation.evaluate((element) => (element as HTMLElement).click());
        await expect(report.locator(`#${controlledId}`)).toHaveCount(1);
        await expect(reports.nth(1 - index).locator(`#${controlledId}`)).toHaveCount(0);

        const internalLink = report.getByRole('link', { name: 'Jump to overview' });
        await expect(internalLink).toHaveAttribute('href', `#${namespace}-overview`);
        await internalLink.evaluate((element) => (element as HTMLElement).click());
        await expect(report.locator(`#${namespace}-overview`)).toBeFocused();
      }
    });
  }

  for (const width of [390, 1440]) {
    test(`renders the capacity dashboard scenario cleanly at ${width}px`, async ({ page }, testInfo) => {
      await page.setViewportSize({ width, height: width === 1440 ? 1200 : 1000 });
      await page.goto('/rich-deliverable-fixture');
      await page.getByRole('tab', { name: 'Capacity dashboard', exact: true }).click();

      const fixture = page.getByTestId('rich-deliverable-fixture');
      const rich = page.getByTestId('rich-deliverable');
      await expect(fixture).toHaveAttribute('data-scenario', 'capacity-dashboard');
      await expect(rich).toHaveAttribute('data-presentation', 'dashboard');
      await expect(rich).toHaveAttribute('data-rich-canvas', 'wide');
      await expect(page.getByText(/Unsupported block:/)).toHaveCount(0);

      // Identity header and status.
      await expect(rich.getByRole('heading', { name: 'Capacity snapshot — Orchid cluster' })).toBeVisible();
      await expect(rich.getByRole('heading', { level: 1 })).toHaveCount(1);
      await expect(rich.getByTestId('rich-deliverable-toolbar')).toHaveClass(/actions-only/);
      await expect(rich.getByText('Idle · 0 open alerts')).toBeVisible();
      await expect(rich.locator('[data-rich-role="metadata"]')).toContainText('orchid-primary');
      await expect(rich.locator('[data-rich-role="status"]')).toContainText('9 suggested indexes');

      // Four compact metrics with accessible progress bars.
      const metricProgressBars = rich.locator('[data-rich-block-type="metric"] [role="progressbar"]');
      await expect(metricProgressBars).toHaveCount(4);
      await expect(metricProgressBars.first()).toHaveAttribute('aria-valuenow', '61');
      await expect(metricProgressBars.first()).toHaveAttribute('aria-valuemax', '100');
      const metricGrid = rich.locator('[data-rich-grid-layout="equal"]').first();
      const metricCards = metricGrid.locator('[data-rich-block-type="metric"]');
      const metricGeometry = await metricCards.evaluateAll((elements) => elements.map((element) => {
        const style = getComputedStyle(element);
        return { height: element.getBoundingClientRect().height, display: style.display };
      }));
      if (width === 1440) {
        const heights = metricGeometry.map(({ height }) => height);
        expect(Math.max(...heights) - Math.min(...heights)).toBeLessThan(0.5);
      } else {
        const mobileGridColumns = await metricGrid.evaluate((element) => getComputedStyle(element).gridTemplateColumns);
        const mobileAnchorDisplay = await metricGrid.locator('.rich-block-anchor').first()
          .evaluate((element) => getComputedStyle(element).display);
        expect(mobileGridColumns.split(' ').length).toBe(1);
        expect(mobileAnchorDisplay).toBe('block');
      }

      // Status chips (generic cards, not a dashboard-only component).
      await expect(rich.getByText('9 suggested indexes')).toBeVisible();
      const actionListStyles = await rich.locator('.rich-card-status .rich-markdown').evaluate((element) => {
        const ordered = element.querySelector('ol')!;
        const unordered = element.querySelector('ul')!;
        return {
          orderedType: getComputedStyle(ordered).listStyleType,
          unorderedType: getComputedStyle(unordered).listStyleType,
          orderedPadding: Number.parseFloat(getComputedStyle(ordered).paddingInlineStart),
          unorderedPadding: Number.parseFloat(getComputedStyle(unordered).paddingInlineStart),
        };
      });
      expect(actionListStyles).toMatchObject({ orderedType: 'decimal', unorderedType: 'disc' });
      expect(actionListStyles.orderedPadding).toBeGreaterThan(0);
      expect(actionListStyles.unorderedPadding).toBeGreaterThan(0);

      // Dense typed recommendation table: typed cells render with their
      // dedicated visual treatment, not raw JSON/text.
      const recommendationTable = rich.locator('[data-rich-block-type="table"]').first();
      await expect(recommendationTable.locator('td[data-cell-type="code"] code')).toHaveCount(5);
      await expect(recommendationTable.locator('td[data-cell-type="badge"] .rich-table-badge')).toHaveCount(5);
      await expect(recommendationTable.locator('td[data-cell-type="number"]')).toHaveCount(5);
      await expect(recommendationTable.getByText('1.08M → 0')).toBeVisible();

      // Mobile tables expose each row as a readable labelled card and hide
      // their sortable header. Verify the desktop interaction through the
      // same generic table primitive instead of forcing an inaccessible
      // mobile-header click.
      if (width === 1440) {
        // Sorting uses the canonical (unwrapped) value, not the typed-cell
        // object or its display label.
        const seenSort = recommendationTable.getByRole('button', { name: /Sort by Times seen/i });
        // The typed recommendation table can use its permitted local
        // horizontal scrollport. The renderer handles this without global
        // page overflow; dispatch the button activation directly so the
        // interaction assertion is not coupled to Playwright's scrollport
        // hit-testing limitation.
        await seenSort.evaluate((element: HTMLButtonElement) => element.click());
        const firstRowSeenCell = recommendationTable.locator('tbody tr').first().locator('td[data-cell-type="number"]');
        await expect(firstRowSeenCell).toHaveText('1');
      }

      // Seven-day traffic comparison and an accessible typed progress cell
      // reused from the same primitive as the metric progress bars.
      await expect(rich.getByText('Traffic — last 7 days')).toBeVisible();
      await expect(rich.getByText('24.1 M (~40/s)')).toBeVisible();

      // Asymmetric action summary: a wide scaling-guidance table spans two
      // tracks beside a narrower "Act now" card.
      const actionGrid = rich.locator('[data-rich-grid-layout="equal"]').last();
      await expect(actionGrid).toBeVisible();
      await expect(rich.getByText('Scaling as data grows')).toBeVisible();
      await expect(rich.getByRole('heading', { name: 'Act now' })).toBeVisible();
      const scalingAnchor = actionGrid.locator('.rich-block-anchor').first();
      const scalingSpan = await scalingAnchor.evaluate((element) => getComputedStyle(element).gridColumn);
      expect(scalingSpan).toBe(width === 390 ? 'span 1' : 'span 2');
      if (width === 1440) {
        const columns = await actionGrid.evaluate((el) => getComputedStyle(el).gridTemplateColumns.split(' ').length);
        expect(columns).toBe(3);
      }

      await expectNoHorizontalClipping(page);

      const cardPath = testInfo.outputPath(`capacity-dashboard-${width}.png`);
      await rich.screenshot({ path: cardPath });
      await testInfo.attach(`capacity-dashboard-${width}.png`, { path: cardPath, contentType: 'image/png' });

      await rich.getByRole('button', { name: 'Open full view' }).click();
      const dialog = page.getByTestId('rich-deliverable-full-view');
      await expect(dialog).toBeVisible();
      await expect(dialog).toHaveAttribute('data-rich-canvas', 'wide');
      if (width === 390) {
        const fullHeaderColumns = await dialog.locator('.rich-section-header').first()
          .evaluate((element) => getComputedStyle(element).gridTemplateColumns);
        expect(fullHeaderColumns.split(' ').length).toBe(1);
      }
      await expectNoHorizontalClipping(page);

      const fullPath = testInfo.outputPath(`capacity-dashboard-full-${width}.png`);
      await page.screenshot({ path: fullPath, fullPage: true });
      await testInfo.attach(`capacity-dashboard-full-${width}.png`, { path: fullPath, contentType: 'image/png' });
    });
  }

  test('stretches generic card peers on desktop and restores content-driven mobile anchors', async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 1000 });
    await page.goto('/rich-deliverable-fixture');
    await page.getByRole('tab', { name: 'Every block type reference', exact: true }).click();
    const cardGrid = page.locator('[data-rich-block-type="card_grid"]').first();
    const cardGeometry = await cardGrid.locator('[data-rich-block-type="card"]').evaluateAll((elements) =>
      elements.map((element) => element.getBoundingClientRect().height)
    );
    expect(Math.max(...cardGeometry) - Math.min(...cardGeometry)).toBeLessThan(0.5);

    await page.setViewportSize({ width: 390, height: 1000 });
    const cardAnchorDisplay = await cardGrid.locator('.rich-block-anchor').first()
      .evaluate((element) => getComputedStyle(element).display);
    expect(cardAnchorDisplay).toBe('block');
  });
});
