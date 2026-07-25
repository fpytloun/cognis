import { execFileSync } from 'node:child_process';
import { expect, test, type Page } from '@playwright/test';

const renderScript = String.raw`
import json
import sys
from types import SimpleNamespace
from cognis.rendering.deliverables import render_standalone_html

fixture = sys.argv[1]
common = {
    "deliverable_id": "dlv_accessibility",
    "version": 1,
    "format": "rich",
    "content": "Fallback",
    "content_hash": "content",
    "rich_hash": "rich",
}
if fixture == "pulse":
    payload = {
        "metadata": {"presentation": "pulse"},
        "blocks": [
            {"type": "hero", "title": "Morning Pulse", "eyebrow": "Live brief", "subtitle": "A dark-theme accessibility fixture.", "badges": ["Current", "Verified"]},
            {"type": "columns", "title": "Today", "blocks": [
                {"type": "day_agenda", "title": "Agenda", "timezone": "UTC", "now": "2026-07-12T07:10:00Z", "items": [
                    {"title": "Review priorities", "start": "2026-07-12T07:00:00Z", "end": "2026-07-12T07:30:00Z", "location": "Studio"},
                    {"title": "Deep work", "start": "2026-07-12T08:00:00Z", "end": "2026-07-12T10:00:00Z"},
                ], "tasks": [{"title": "Confirm one priority"}], "source": {"title": "Calendar", "url": "https://example.test/calendar", "refreshed_at": "07:05 UTC"}},
                {"type": "card", "title": "Watch", "content": "Keep the important context readable.", "eyebrow": "Signal"},
            ]},
            {"type": "chart", "title": "Trend", "description": "Representative chart fallback table.", "source": "Fixture dataset", "timestamp": "07:00 UTC", "rows": [{"Metric": "Readiness", "Value": "82"}, {"Metric": "Focus", "Value": "High"}]},
            {"type": "source_list", "title": "Sources"},
        ],
        "sources": [{"title": "Fixture source", "url": "https://example.test/source", "description": "Provenance remains legible."}],
    }
else:
    payload = {
        "metadata": {"toc": {"enabled": True, "depth": 4}},
        "blocks": [
            {"type": "hero", "title": "Research Report", "eyebrow": "Evidence review", "subtitle": "Representative publication fixture.", "badges": ["Reviewed"]},
            {"type": "research_answer", "title": "Summary", "description": "Metadata must remain readable.", "paragraphs": [{"text": "The semantic palette supports accessible standalone reading.", "citations": ["source"]}], "key_points": ["Normal text meets WCAG AA."]},
            {"type": "section", "title": "Analysis", "children": [
                {"type": "section", "title": "Details", "children": [
                    {"type": "section", "title": "Edge cases", "content": "Nested hierarchy remains compact."},
                ]},
            ]},
            {"type": "evidence_report", "title": "Evidence", "claims": [{"label": "Finding", "title": "Contrast is semantic", "content": "Foregrounds follow theme variables.", "confidence": 0.95, "evidence": [{"text": "Dark navy and cyan preserve readable contrast.", "source": "Audit fixture"}], "citations": ["source"]}]},
            {"type": "metric", "title": "Coverage", "label": "Selectors audited", "value": "100", "unit": "%", "delta": "complete", "description": "Status metadata remains readable."},
            {"type": "table", "title": "Results", "caption": "Representative table text.", "rows": [{"Surface": "Navy", "Text": "Light cyan", "Status": "Pass"}]},
            {"type": "code", "title": "Code", "language": "css", "content": "color: var(--text);"},
            {"type": "figure", "title": "Fallback", "src": "https://example.test/figure.png", "caption": "Caption and source metadata.", "source": "Fixture source", "source_url": "https://example.test/source"},
        ],
        "sources": [{"id": "source", "title": "Fixture source", "url": "https://example.test/source", "publisher": "Cognis"}],
    }
row = SimpleNamespace(title=payload["blocks"][0]["title"], rich_payload=payload, **common)
sys.stdout.write(render_standalone_html(row, download_pdf_url="/fixture.pdf"))
`;

function renderFixture(name: 'pulse' | 'research') {
  return execFileSync('uv', ['run', 'python', '-c', renderScript, name], {
    cwd: '..',
    encoding: 'utf8',
    maxBuffer: 16 * 1024 * 1024,
  });
}

async function loadTheme(page: Page, html: string, mode: 'light' | 'dark' | 'system-dark') {
  await page.emulateMedia({ colorScheme: mode === 'system-dark' ? 'dark' : 'light' });
  await page.goto('/rich-deliverable-fixture');
  await page.evaluate((choice) => {
    if (choice === 'light' || choice === 'dark') localStorage.setItem('cognis-deliverable-theme', choice);
    else localStorage.removeItem('cognis-deliverable-theme');
    const dark = choice === 'dark' || (choice === 'system' && matchMedia('(prefers-color-scheme: dark)').matches);
    document.documentElement.style.background = dark ? '#020617' : '#f5f7fa';
    document.body.style.cssText = `transition:none;background:${dark ? '#020617' : '#f5f7fa'};color:${dark ? '#e5f4ff' : '#18212f'}`;
  }, mode === 'system-dark' ? 'system' : mode);
  await page.setContent(html, { waitUntil: 'load' });
  await expect(page.locator('html')).toHaveAttribute(
    'data-resolved-theme',
    mode === 'light' ? 'light' : 'dark',
  );
  await expect(page.locator('html')).toHaveAttribute(
    'data-theme',
    mode === 'system-dark' ? 'system' : mode,
  );
  await expect.poll(() => page.locator('.document').evaluate((element) => getComputedStyle(element).backgroundColor))
    .toBe(mode === 'light' ? 'rgb(255, 255, 255)' : 'rgb(8, 21, 37)');
}

async function expectWcagTextContrast(page: Page) {
  const failures = await page.evaluate(() => {
    const parse = (value: string) => {
      const values = value.match(/[\d.]+/g)?.map(Number) ?? [];
      return { r: values[0] ?? 0, g: values[1] ?? 0, b: values[2] ?? 0, a: values[3] ?? 1 };
    };
    const luminance = ({ r, g, b }: { r: number; g: number; b: number }) => {
      const channel = (value: number) => {
        const normalized = value / 255;
        return normalized <= 0.04045 ? normalized / 12.92 : ((normalized + 0.055) / 1.055) ** 2.4;
      };
      return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b);
    };
    const background = (element: Element) => {
      let current: Element | null = element;
      while (current) {
        const color = parse(getComputedStyle(current).backgroundColor);
        if (color.a > 0.99) return color;
        current = current.parentElement;
      }
      return parse(getComputedStyle(document.body).backgroundColor);
    };
    return Array.from(document.querySelectorAll<HTMLElement>('body *'))
      .filter((element) => {
        const style = getComputedStyle(element);
        const rect = element.getBoundingClientRect();
        const ownText = Array.from(element.childNodes).some(
          (node) => node.nodeType === Node.TEXT_NODE && node.textContent?.trim(),
        );
        return ownText && style.visibility !== 'hidden' && style.display !== 'none' && rect.width > 0 && rect.height > 0;
      })
      .map((element) => {
        const style = getComputedStyle(element);
        const foreground = parse(style.color);
        const bg = background(element);
        const light = Math.max(luminance(foreground), luminance(bg));
        const dark = Math.min(luminance(foreground), luminance(bg));
        const ratio = (light + 0.05) / (dark + 0.05);
        const large = Number.parseFloat(style.fontSize) >= 24
          || (Number.parseFloat(style.fontSize) >= 18.66 && Number.parseInt(style.fontWeight, 10) >= 700);
        return { tag: element.tagName, className: element.className, text: element.innerText.trim().slice(0, 80), ratio, required: large ? 3 : 4.5 };
      })
      .filter((result) => result.ratio + 0.01 < result.required);
  });
  expect(failures, JSON.stringify(failures, null, 2)).toEqual([]);
}

test.describe('standalone generated HTML dark accessibility', () => {
  const fixtures = {
    pulse: renderFixture('pulse'),
    research: renderFixture('research'),
  };

  for (const fixtureName of ['pulse', 'research'] as const) {
    for (const mode of ['light', 'dark', 'system-dark'] as const) {
      test(`${fixtureName} passes contrast in ${mode}`, async ({ page }) => {
        await page.setViewportSize({ width: 1440, height: 1000 });
        await loadTheme(page, fixtures[fixtureName], mode);
        await expect(page.locator('.document')).toBeVisible();
        await expectWcagTextContrast(page);

        const action = page.getByRole('button', { name: /Theme:/ });
        const base = await action.evaluate((element) => getComputedStyle(element).borderColor);
        await action.hover();
        await expect.poll(() => action.evaluate((element) => getComputedStyle(element).borderColor))
          .not.toBe(base);
        await action.focus();
        await expect(action).toBeFocused();
        expect(await action.evaluate((element) => getComputedStyle(element).outlineStyle)).not.toBe('none');
      });
    }
  }

  for (const width of [390, 1440]) {
    test(`captures standalone pulse dark at ${width}px`, async ({ page }, testInfo) => {
      await page.setViewportSize({ width, height: width === 390 ? 844 : 1000 });
      await loadTheme(page, fixtures.pulse, 'dark');
      await expectWcagTextContrast(page);
      const path = testInfo.outputPath(`standalone-pulse-dark-${width}.png`);
      await page.screenshot({ path, fullPage: true });
      await testInfo.attach(`standalone-pulse-dark-${width}.png`, {
        path,
        contentType: 'image/png',
      });
    });
  }

  test('research standalone keeps one title and a compact desktop hierarchy', async ({ page }, testInfo) => {
    await page.setViewportSize({ width: 1440, height: 1000 });
    await loadTheme(page, fixtures.research, 'light');

    const article = page.locator('article.document');
    const toc = page.locator('.document-toc');
    await expect(article.locator(':scope > .document-header')).toBeVisible();
    await expect(article.getByRole('heading', { level: 1, name: 'Research Report' })).toHaveCount(1);
    await expect(page.getByRole('heading', { level: 1 })).toHaveCount(1);
    await expect(toc).toBeVisible();
    await expect(toc.locator('li[data-level="2"] > ol li[data-level="3"]')).toContainText('Details');
    await expect(toc.locator('li[data-level="3"] > ol li[data-level="4"]')).toContainText('Edge cases');
    await expect(toc.locator('[class*="badge"], [class*="card"]')).toHaveCount(0);
    await expect(page.getByRole('button', { name: 'Open table of contents' })).toBeHidden();
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(1440);

    const path = testInfo.outputPath('standalone-research-desktop.png');
    await page.screenshot({ path, fullPage: true });
    await testInfo.attach('standalone-research-desktop.png', { path, contentType: 'image/png' });
  });

  test('research standalone exposes an accessible 390px TOC drawer beside document actions', async ({ page }, testInfo) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await loadTheme(page, fixtures.research, 'dark');

    const actions = page.getByRole('navigation', { name: 'Document actions' });
    const trigger = actions.getByRole('button', { name: 'Open table of contents' });
    await expect(trigger).toBeVisible();
    await expect(actions.getByRole('link', { name: 'Download PDF' })).toBeVisible();
    await expect(actions.getByRole('button', { name: /Theme:/ })).toBeVisible();
    await trigger.focus();
    await trigger.click();

    const dialog = page.getByRole('dialog', { name: 'Table of contents' });
    await expect(dialog).toBeVisible();
    await expect(dialog.getByRole('button', { name: 'Close table of contents' })).toBeFocused();
    await page.locator('[data-toc-backdrop]').click({ position: { x: 8, y: 8 } });
    await expect(dialog).toHaveCount(0);
    await expect(trigger).toBeFocused();

    await trigger.click();
    await expect(dialog).toBeVisible();
    await page.keyboard.press('Escape');
    await expect(dialog).toHaveCount(0);
    await expect(trigger).toBeFocused();

    await trigger.click();
    await dialog.getByRole('link', { name: 'Details' }).click();
    await expect(dialog).toHaveCount(0);
    await expect(page.locator('#details')).toBeFocused();
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(390);

    const path = testInfo.outputPath('standalone-research-mobile-390.png');
    await page.screenshot({ path, fullPage: true });
    await testInfo.attach('standalone-research-mobile-390.png', { path, contentType: 'image/png' });
  });
});
