import { describe, expect, it } from 'vitest';
import { compareDecimals, decimalShare } from './decimal';
import { costBandKey, costComposition, hasCostCohortDrill, overviewCostBands } from './composition';
import { compositionBand } from './composition.test-data';
import { fixtureQuery } from './fixtures';
import { fixtureScreen } from './screen-fixture';
import type { UsageCostMarginal } from './model';

describe('known priced subtotal composition', () => {
  it('requires a representable predicate rather than treating an empty drill as overall usage', () => {
    const cohort = { key: 'protected', label: 'Protected cohort', amount: '1', drill: {} };
    expect(hasCostCohortDrill(cohort)).toBe(false);
    expect(hasCostCohortDrill({ ...cohort, drill: { filters: { agent: '' } } })).toBe(false);
    expect(hasCostCohortDrill({ ...cohort, drill: { filters: { agent: 'synthetic-agent-alpha' } } })).toBe(true);
    expect(hasCostCohortDrill({ ...cohort, drill: { selectionRef: 'v2.screen-fixture-session-1', requiresSelection: true } })).toBe(true);
    expect(hasCostCohortDrill({ ...cohort, drill: { detailFilters: { agent_ids: [] } } })).toBe(false);
    expect(hasCostCohortDrill({ ...cohort, amount: null, drill: { filters: { agent: 'synthetic-agent-alpha' } } })).toBe(false);
  });
  it('includes known partial amounts without pricing unknown cohorts as zero', () => {
    const band = compositionBand();
    const composition = costComposition(band, 'user');
    expect(composition.reason).toBeNull();
    expect(composition.sectors).toHaveLength(2);
    expect(composition.rows.map((row) => [row.cohort.label, row.percentage])).toEqual([
      ['user Alpha', '75%'], ['Unallocated / redacted', '25%'], ['user real zero', '0%'], ['user unpriced only', 'Unavailable']
    ]);
    expect(composition.rows[1].cohort.drill).toBeNull();
    expect(composition.rows[3].share).toBeNull();
    expect(band.coverage).toEqual({ priced: 4, partial: 2, unpriced: 3, unallocatable: 1 });
  });
  it('requires complete marginal membership and exact reconciliation, not complete pricing', () => {
    const band = compositionBand();
    const marginal = band.dimensions.agent;
    if (marginal.state !== 'ready') throw new Error('Expected ready test input');
    marginal.complete = false;
    expect(costComposition(band, 'agent')).toMatchObject({ sectors: [], reason: expect.stringContaining('groups are missing') });
    marginal.complete = true;
    marginal.groups[0].amount = '7.5';
    expect(costComposition(band, 'agent')).toMatchObject({ sectors: [], reason: expect.stringContaining('differ from the total') });
    expect(costComposition(band, 'agent').rows.every((row) => row.share === null)).toBe(true);
  });
  it('groups only complete returned positive amounts into Other and preserves every data row', () => {
    const band = compositionBand();
    band.amount = '36';
    band.dimensions.model = { state: 'ready', complete: true, groups: Array.from({ length: 8 }, (_, i) => ({
      key: String(i), label: `Model ${i}`, amount: String(i + 1), drill: null
    })) };
    const composition = costComposition(band, 'model');
    expect(composition.rows).toHaveLength(8);
    expect(composition.sectors).toHaveLength(6);
    expect(composition.sectors.at(-1)).toMatchObject({ aggregated: true, cohort: { amount: '6', drill: null } });
    expect(composition.sectors.reduce((sum, sector) => sum + sector.share!, 0)).toBeCloseTo(1, 10);
    expect(composition.sectors[1].start).toBe(composition.sectors[0].share);
  });
  it.each([
    [null, 'unavailable'], ['0', 'No priced cost'], ['-1', 'negative costs'], ['not-money', 'Cost data is invalid']
  ])('keeps unavailable, zero and signed totals distinct: %s', (amount, message) => {
    const band = compositionBand();
    band.amount = amount;
    if (amount === '0') {
      band.dimensions.provider = { state: 'ready', complete: true, groups: [{ key: 'zero', label: 'Zero', amount: '0', drill: null }] };
    }
    expect(costComposition(band, 'provider')).toMatchObject({ sectors: [], reason: expect.stringContaining(message) });
  });
  it('preserves explicit per-dimension unavailability without substituting another dimension', () => {
    const band = compositionBand();
    band.dimensions.user = { state: 'unavailable', reason: 'User grouping is not authorized.' };
    expect(costComposition(band, 'user')).toMatchObject({ rows: [], sectors: [], reason: 'User grouping is not authorized.' });
    expect(costComposition(band, 'provider').sectors).toHaveLength(2);
  });
  it('keeps currency and basis bands separate even when the selected band has no bucket observation', () => {
    const view = fixtureScreen(fixtureQuery);
    const usd = compositionBand(), eur = compositionBand('EUR'), equivalent = compositionBand('USD', 'api_equivalent');
    view.costBreakdowns = [usd, eur, equivalent];
    expect(overviewCostBands(view).map((band) => band.key)).toEqual(expect.arrayContaining([
      costBandKey(usd), costBandKey(eur), costBandKey(equivalent)
    ]));
    expect(new Set(overviewCostBands(view).map((band) => band.key)).size).toBe(overviewCostBands(view).length);
    expect(costComposition(eur, 'user').total).toBe('€10.00');
  });
  it('calculates ratios without overflowing or losing decimal reconciliation precision', () => {
    expect(compareDecimals('9007199254740993.000000000001', '9007199254740993.000000000000')).toBe(1);
    expect(decimalShare(`5${'0'.repeat(350)}`, `1${'0'.repeat(351)}`)).toBe(.5);
    expect(decimalShare('0', '0')).toBeNull();
    const band = compositionBand();
    band.amount = '1';
    const groups: Extract<UsageCostMarginal, { state: 'ready' }> = { state: 'ready', complete: true, groups: [
      { key: 'tiny', label: 'Tiny', amount: '0.000000000000000001', drill: null },
      { key: 'rest', label: 'Rest', amount: '0.999999999999999999', drill: null }
    ] };
    band.dimensions.user = groups;
    expect(costComposition(band, 'user').rows[1].percentage).toBe('<0.1%');
  });
});
