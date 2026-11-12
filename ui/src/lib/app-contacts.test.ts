import { describe, expect, it } from 'vitest';
import { appContactBindingId } from './app-contacts';

describe('app contact presentation', () => {
  it('recognizes the native client route without inferring access from agent sharing', () => {
    expect(appContactBindingId(null)).toBeNull();
    expect(appContactBindingId({
      context: { type: 'web', ref: null, memory_labels: {}, platform_data: {} },
    })).toBeNull();
    expect(appContactBindingId({
      context: { type: 'web', ref: null, memory_labels: {}, platform_data: { app_contact_binding_id: 'managed-channel' } },
    })).toBe('managed-channel');
    expect(appContactBindingId({
      context: { type: 'web', ref: null, memory_labels: {}, platform_data: { app_contact_binding_id: true } },
    })).toBeNull();
  });
});
