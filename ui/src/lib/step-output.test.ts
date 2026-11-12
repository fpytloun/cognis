import { describe, expect, it } from 'vitest';

import { deliverableRelationNote } from './step-output';

describe('deliverableRelationNote', () => {
  it('explains an approved deliverable that belongs to a failed step', () => {
    expect(deliverableRelationNote('failed', { state: 'current', replacement_status: null, label: 'Current output' }))
      .toBe('The step wrote this deliverable before it stopped. It is still the latest output of this step.');
  });

  it('uses the backend freshness label for replaced output', () => {
    expect(deliverableRelationNote('completed', { state: 'stale', replacement_status: 'running', label: 'Stale — revision 1, new revision in progress' }))
      .toBe('Stale — revision 1, new revision in progress. A later attempt of this step is replacing it.');
    expect(deliverableRelationNote('failed', { state: 'historical', replacement_status: null, label: 'Historical — revision 2' }))
      .toBe('Historical — revision 2. A later attempt produced a newer output.');
  });

  it('adds nothing for a completed step with its current output', () => {
    expect(deliverableRelationNote('completed', { state: 'current', replacement_status: null, label: 'Current output' })).toBeNull();
    expect(deliverableRelationNote('approved', null)).toBeNull();
  });
});
