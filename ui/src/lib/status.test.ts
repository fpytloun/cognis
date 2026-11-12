import { describe, expect, it } from 'vitest';
import { statusPresentation } from './status';
describe('product status vocabulary', () => {
  it('never renders unknown, stale, disabled or unrecognized state as healthy', () => {
    for (const status of ['unknown', 'stale', 'disabled', 'unrecognized']) {
      expect(statusPresentation(status).className).not.toContain('success');
    }
  });
  it('pairs meaningful labels with every status colour', () => {
    expect(statusPresentation('failed')).toMatchObject({ label: 'Failed', icon: '×' });
    expect(statusPresentation('inflight').label).toBe('In progress');
    expect(statusPresentation('partial').className).toContain('warning');
  });
  it('adapts status to the narrower Stat tone vocabulary', () => {
    expect(statusPresentation('failed').statTone).toBe('danger');
    expect(statusPresentation('partial').statTone).toBe('warning');
    expect(statusPresentation('unknown').statTone).toBe('unknown');
    expect(statusPresentation('ready').statTone).toBe('neutral');
    expect(statusPresentation('inflight').statTone).toBe('neutral');
  });
});
