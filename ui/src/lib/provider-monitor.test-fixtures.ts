import type { ProviderUsage } from '$lib/types/api';

/** Canonical subscription-shaped sample shared by component and browser checks. */
export function elevenlabsUsage(): ProviderUsage {
  return {
    provider_id: 'elevenlabs', ok: true, source: 'elevenlabs_subscription_usage',
    usage_url: null, fetched_at: null, plan_type: 'free', primary: null, secondary: null,
    credits: null, rate_limit_reached_type: null, allowed: true, limit_reached: false,
    additional_rate_limits: [{
      limit_id: 'subscription_credits', limit_name: 'Subscription credits',
      secondary: null, allowed: null, limit_reached: null,
      primary: {
        used_percent: 15.36, limit: 10000, remaining: 8464,
        resets_at: '2026-12-01T00:00:00Z', window_duration_mins: null,
        reset_after_seconds: null
      }
    }]
  };
}
