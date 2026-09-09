import { expect, test } from '@playwright/test';

const scenarios = [
  {
    id: 'wearable-recovery',
    title: 'Synthetic wearable recovery review',
    safety: 'supports reflection on routine and recovery',
    source: 'Synthetic Oura-style CSV export',
    sourcePath: '/fixtures/rich-scenarios/wearable-recovery/source-export.json',
    charts: ['Readiness', 'Sleep score'],
    value: '97%',
  },
  {
    id: 'menstrual-cycle',
    title: 'Synthetic menstrual-cycle pattern review',
    safety: 'not contraception or a diagnosis',
    source: 'Synthetic cycle diary and wearable export',
    sourcePath: '/fixtures/rich-scenarios/menstrual-cycle/source-export.json',
    charts: [],
    value: 'Sep 12–15',
  },
  {
    id: 'data-projections',
    title: 'Synthetic home solar projection',
    safety: 'Forecasts are estimates',
    source: 'Synthetic household-meter export and solar model',
    sourcePath: '/fixtures/rich-scenarios/data-projections/source-export.json',
    charts: ['Forecast generation range (kWh)', 'Expected generation (kWh)'],
    value: '8.1 years',
  },
] as const;

async function loadScenario(page: import('@playwright/test').Page, id: string) {
  await page.goto(`/rich-deliverable-fixture?scenario=${id}&surface=standalone`, {
    waitUntil: 'domcontentloaded',
  });
  await expect(page.getByTestId('rich-deliverable-fixture')).toHaveAttribute('data-scenario', id, {
    timeout: 30_000,
  });
  await expect(page.locator('.rich-chart-canvas.chart-ready').first()).toBeVisible();
}

test.describe('health and projection rich scenarios', () => {
  test('wide embedded TOC restores the full body width after close', async ({ page }) => {
    await page.setViewportSize({ width: 1280, height: 1000 });
    await page.goto('/rich-deliverable-fixture?scenario=wearable-recovery&surface=embedded&theme=light&width=1280', {
      waitUntil: 'domcontentloaded',
    });
    const rich = page.getByTestId('rich-deliverable');
    const document = page.getByTestId('rich-deliverable-inline-document');
    const body = page.getByTestId('rich-deliverable-body');
    await expect(page.getByTestId('rich-deliverable-fixture')).toHaveAttribute('data-scenario', 'wearable-recovery');
    await expect(page.getByRole('button', { name: 'Open table of contents' })).toBeVisible();
    await expect(rich).toHaveAttribute('data-inline-toc-layout', 'sidebar');

    const closedWidth = await body.evaluate((element) => element.getBoundingClientRect().width);
    const documentWidth = await document.evaluate((element) => element.getBoundingClientRect().width);
    expect(closedWidth / documentWidth).toBeGreaterThan(0.95);

    await page.getByRole('button', { name: 'Open table of contents' }).click();
    await expect(page.getByTestId('rich-deliverable-toc')).toBeVisible();
    const openWidth = await body.evaluate((element) => element.getBoundingClientRect().width);
    expect(openWidth).toBeLessThan(closedWidth - 100);

    await rich.locator('.rich-toc > header button[aria-label="Close table of contents"]').click();
    await expect(document).not.toHaveClass(/inline-toc-sidebar/);
    await expect(page.getByRole('button', { name: 'Open table of contents' })).toBeFocused();
    const restoredWidth = await body.evaluate((element) => element.getBoundingClientRect().width);
    expect(restoredWidth).toBeGreaterThan(closedWidth - 1);
    expect(restoredWidth / documentWidth).toBeGreaterThan(0.95);
  });

  for (const scenario of scenarios) {
    test(`${scenario.id} renders data, provenance, uncertainty, and chart controls`, async ({ page }) => {
      await page.setViewportSize({ width: 1440, height: 1000 });
      await loadScenario(page, scenario.id);

      await expect(page.getByText(scenario.title, { exact: true }).first()).toBeVisible();
      await expect(page.getByText(scenario.safety, { exact: false }).first()).toBeVisible();
      await expect(page.getByText(scenario.source, { exact: true }).first()).toBeVisible();
      await expect(page.getByRole('link', { name: scenario.source }).last()).toHaveAttribute(
        'href',
        new RegExp(`${scenario.sourcePath}$`),
      );
      expect(await page.evaluate(async (sourcePath) => {
        const response = await fetch(sourcePath);
        const data = await response.json() as { synthetic?: boolean; source?: string };
        return response.ok && data.synthetic === true && data.source;
      }, scenario.sourcePath)).toBe(scenario.source);
      await expect(page.getByText(/generated 2026-09-05/i).first()).toBeVisible();
      await expect(page.getByText(scenario.value, { exact: true }).first()).toBeVisible();

      const legends = page.getByRole('group', { name: 'Chart series' });
      await expect(legends.first()).toBeVisible();
      for (const label of scenario.charts) {
        await expect(page.getByRole('button', { name: label }).first()).toBeVisible();
      }
    });
  }

  for (const width of [390, 1440]) {
    test(`health and projection scenarios have no horizontal overflow at ${width}px`, async ({ page }) => {
      await page.setViewportSize({ width, height: width === 390 ? 844 : 1000 });
      for (const scenario of scenarios) {
        await loadScenario(page, scenario.id);
        expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
      }
    });
  }

  test('projection scenario exposes range semantics and its observed-versus-forecast limitation', async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 1000 });
    await loadScenario(page, 'data-projections');

    await expect(page.getByRole('button', { name: 'Forecast generation range (kWh)' })).toBeVisible();
    await expect(page.getByRole('button', { name: 'Expected generation (kWh)' })).toBeVisible();
    await expect(page.getByText(/Observed-versus-forecast styling is not sufficient/i)).toBeVisible();
    await expect(page.getByText(/6.5–10.6 years/)).toBeVisible();
  });
});
