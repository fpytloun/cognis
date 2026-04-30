<script lang="ts">
  import Button from '$lib/components/ui/Button.svelte';
  import Input from '$lib/components/ui/Input.svelte';
  import { api, asApiError } from '$lib/api/client';
  import type { Notification } from '$lib/types/api';

  let { notification, compact = false, onResolved } = $props<{
    notification: Notification;
    compact?: boolean;
    onResolved?: (decision: string) => void | Promise<void>;
  }>();

  let values = $state<Record<string, string>>({});
  let customInput = $state('');
  let useCustomInput = $state(false);
  let submitting = $state(false);
  let error = $state('');

  const payload = $derived(notification.payload ?? {});
  const credentialId = $derived(typeof payload.credential_id === 'string' ? payload.credential_id : '');
  const kind = $derived(typeof payload.kind === 'string' ? payload.kind : 'text');
  const label = $derived(typeof payload.label === 'string' ? payload.label : 'Credential required');
  const message = $derived(
    typeof payload.message === 'string' && payload.message.trim()
      ? payload.message
      : typeof payload.description === 'string'
        ? payload.description
        : 'This credential is needed to continue.'
  );
  const fields = $derived.by(() => {
    const required = payload.required_fields;
    if (Array.isArray(required)) {
      const names = required.filter((item): item is string => typeof item === 'string' && item.trim().length > 0);
      if (names.length > 0) return names;
    }
    if (kind === 'username_password') return ['username', 'password'];
    if (kind === 'token') return ['token'];
    return ['value'];
  });
  const canApprove = $derived(
    useCustomInput
      ? customInput.trim().length > 0
      : fields.every((field) => (values[field] ?? '').trim().length > 0)
  );

  function inputType(field: string): string {
    const lower = field.toLowerCase();
    if (lower.includes('password') || lower.includes('token') || lower.includes('secret') || lower.includes('seed')) {
      return 'password';
    }
    return 'text';
  }

  function autocomplete(field: string): 'username' | 'current-password' | 'off' {
    const lower = field.toLowerCase();
    if (lower.includes('password')) return 'current-password';
    if (lower === 'username' || lower === 'email') return 'username';
    return 'off';
  }

  function setField(field: string, value: string): void {
    values = { ...values, [field]: value };
  }

  async function resolve(decision: 'approve' | 'deny' | 'cancel'): Promise<void> {
    error = '';
    submitting = true;
    try {
      if (decision === 'approve') {
        if (useCustomInput) {
          await api.notifications.resolve(notification.notification_id, {
            decision,
            response: customInput,
          });
        } else {
          const credentialPayload = Object.fromEntries(fields.map((field) => [field, values[field] ?? '']));
          await api.notifications.resolve(notification.notification_id, {
            decision,
            credential: {
              credential_id: credentialId,
              kind,
              label,
              payload: credentialPayload,
              metadata: {},
              scope: typeof payload.scope === 'string' ? payload.scope : 'user',
              agent_id: typeof payload.agent_id === 'string' ? payload.agent_id : null,
              description: typeof payload.description === 'string' ? payload.description : null,
              expires_at: null,
            },
          });
        }
      } else {
        await api.notifications.resolve(notification.notification_id, { decision });
      }
      await onResolved?.(decision);
    } catch (caughtError) {
      error = asApiError(caughtError).message;
    } finally {
      submitting = false;
    }
  }
</script>

<article class={`rounded-3xl border border-amber-400/30 bg-amber-400/10 text-amber-50 shadow-card ${compact ? 'px-4 py-3' : 'px-5 py-5'}`}>
  <div class="flex flex-wrap items-start justify-between gap-3">
    <div>
      <p class="text-xs font-semibold uppercase tracking-[0.25em] text-amber-200">Credential required</p>
      <h3 class="mt-1 text-base font-semibold text-white">{label}</h3>
    </div>
    <span class="rounded-full border border-amber-300/40 px-2.5 py-1 text-[11px] font-medium uppercase tracking-[0.2em] text-amber-100">{kind}</span>
  </div>

  <p class="mt-3 text-sm leading-6 text-amber-50/90">{message}</p>
  <p class="mt-2 text-xs leading-5 text-amber-100/70">Values are sent directly to Cognis encrypted credential storage and are not shown to the model.</p>

  <div class="mt-4 space-y-3">
    <label class="flex items-center gap-2 text-xs text-amber-100/80">
      <input bind:checked={useCustomInput} class="rounded border-amber-300/40 bg-slate-950 text-amber-300" type="checkbox" disabled={submitting} />
      <span>Provide custom input instead of structured fields</span>
    </label>

    {#if useCustomInput}
      <textarea bind:value={customInput} class="min-h-[110px] w-full rounded-2xl border border-amber-300/30 bg-slate-950/80 px-4 py-3 text-sm text-slate-100 placeholder:text-slate-500" placeholder="Paste the credential in the requested format"></textarea>
    {:else}
      <div class="grid gap-3 sm:grid-cols-2">
        {#each fields as field}
          <label class="space-y-1.5 text-sm font-medium text-amber-50">
            <span class="capitalize">{field.replaceAll('_', ' ')}</span>
            <Input
              autocomplete={autocomplete(field)}
              type={inputType(field)}
              value={values[field] ?? ''}
              disabled={submitting}
              oninput={(event: Event) => setField(field, (event.currentTarget as HTMLInputElement).value)}
            />
          </label>
        {/each}
      </div>
    {/if}
  </div>

  {#if error}
    <p class="mt-3 rounded-2xl border border-rose-400/40 bg-rose-500/10 px-4 py-3 text-sm text-rose-100">{error}</p>
  {/if}

  <div class="mt-4 flex flex-wrap gap-2">
    <Button size="sm" disabled={submitting || !canApprove} onclick={() => resolve('approve')}>Save and resume</Button>
    <Button size="sm" variant="secondary" disabled={submitting} onclick={() => resolve('cancel')}>Cancel request</Button>
    <Button size="sm" variant="danger" disabled={submitting} onclick={() => resolve('deny')}>Deny</Button>
  </div>
</article>
