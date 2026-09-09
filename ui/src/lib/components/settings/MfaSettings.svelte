<script lang="ts">
  import { goto } from '$app/navigation';
  import { onMount } from 'svelte';
  import QRCode from 'qrcode';

  import { apiUrl } from '$lib/config';
  import { auth } from '$lib/stores/auth';
  import Button from '$lib/components/ui/Button.svelte';
  import Input from '$lib/components/ui/Input.svelte';

  interface MfaStatus {
    enabled: boolean;
    policy: 'optional' | 'required';
    recovery_codes_remaining: number;
  }

  interface Setup {
    challenge_token: string;
    secret: string;
    provisioning_uri: string;
  }

  let status: MfaStatus | null = null;
  let setup: Setup | null = null;
  let qrDataUrl = '';
  let code = '';
  let currentPassword = '';
  let recoveryCodes: string[] = [];
  let error = '';
  let busy = false;

  async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
    const response = await fetch(apiUrl(path), {
      credentials: 'include',
      ...init,
      headers: { 'Content-Type': 'application/json', ...(init.headers ?? {}) }
    });
    if (!response.ok) {
      const payload = await response.json().catch(() => ({})) as { detail?: string };
      throw new Error(payload.detail ?? 'The MFA request failed.');
    }
    return (await response.json()) as T;
  }

  async function load(): Promise<void> {
    status = await request<MfaStatus>('/api/auth/mfa');
  }

  async function startEnrollment(): Promise<void> {
    busy = true;
    error = '';
    try {
      setup = await request<Setup>('/api/auth/mfa/enroll/start', {
        method: 'POST',
        body: JSON.stringify({ current_password: currentPassword })
      });
      qrDataUrl = await QRCode.toDataURL(setup.provisioning_uri, { width: 220, margin: 1 });
    } catch (caught) {
      error = caught instanceof Error ? caught.message : 'Unable to start MFA setup.';
    } finally {
      busy = false;
    }
  }

  async function confirmEnrollment(): Promise<void> {
    if (!setup) return;
    busy = true;
    error = '';
    try {
      const response = await request<{ recovery_codes: string[] }>('/api/auth/mfa/enroll/confirm', {
        method: 'POST',
        body: JSON.stringify({ challenge_token: setup.challenge_token, code })
      });
      recoveryCodes = response.recovery_codes;
      setup = null;
      await load();
    } catch (caught) {
      error = caught instanceof Error ? caught.message : 'Unable to enable MFA.';
    } finally {
      busy = false;
    }
  }

  async function manage(path: string): Promise<void> {
    busy = true;
    error = '';
    try {
      const response = await request<{ recovery_codes?: string[] }>(path, {
        method: 'POST',
        body: JSON.stringify({ current_password: currentPassword, code })
      });
      recoveryCodes = response.recovery_codes ?? [];
      if (recoveryCodes.length === 0) {
        await auth.logout();
        await goto('/login');
      }
    } catch (caught) {
      error = caught instanceof Error ? caught.message : 'Unable to update MFA.';
    } finally {
      busy = false;
    }
  }

  function downloadRecoveryCodes(): void {
    const blob = new Blob([`${recoveryCodes.join('\n')}\n`], { type: 'text/plain' });
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement('a');
    anchor.href = url;
    anchor.download = 'cognis-recovery-codes.txt';
    anchor.click();
    URL.revokeObjectURL(url);
  }

  onMount(() => {
    void load().catch((caught: unknown) => {
      error = caught instanceof Error ? caught.message : 'Unable to load MFA status.';
    });
  });
</script>

<div class="space-y-4 border-t border-slate-800 pt-5">
  <div>
    <p class="text-xs uppercase tracking-[0.25em] text-slate-400">Multi-factor authentication</p>
    <h3 class="mt-1 font-semibold text-white">Authenticator app</h3>
    {#if status}
      <p class="text-sm text-slate-400">
        {status.enabled ? `Enabled · ${status.recovery_codes_remaining} recovery codes remain` : 'Not enabled'}
        · Policy: {status.policy}
      </p>
    {/if}
  </div>

  {#if recoveryCodes.length > 0}
    <div class="rounded-2xl border border-emerald-500/30 bg-emerald-500/10 p-4">
      <p class="font-medium text-emerald-100">Save these recovery codes now</p>
      <pre class="mt-3 text-sm text-emerald-50">{recoveryCodes.join('\n')}</pre>
      <div class="mt-3 flex gap-2">
        <Button size="sm" variant="secondary" onclick={downloadRecoveryCodes}>Download</Button>
        <Button size="sm" onclick={async () => { recoveryCodes = []; await auth.logout(); await goto('/login'); }}>I saved them</Button>
      </div>
    </div>
  {:else if setup}
    <div class="space-y-3 rounded-2xl border border-slate-700 p-4">
      {#if qrDataUrl}<img class="mx-auto rounded-xl bg-white p-2" src={qrDataUrl} alt="TOTP setup QR code" />{/if}
      <p class="break-all font-mono text-xs text-slate-400">{setup.secret}</p>
      <Input bind:value={code} autocomplete="one-time-code" inputmode="numeric" placeholder="123456" />
      <Button onclick={confirmEnrollment} disabled={busy}>Confirm and enable</Button>
    </div>
  {:else if status?.enabled}
    <div class="grid gap-3 md:grid-cols-2">
      <Input bind:value={currentPassword} type="password" autocomplete="current-password" placeholder="Current password" />
      <Input bind:value={code} autocomplete="one-time-code" placeholder="TOTP or recovery code" />
      <Button variant="secondary" onclick={() => manage('/api/auth/mfa/recovery-codes/regenerate')} disabled={busy}>
        Regenerate recovery codes
      </Button>
      {#if status.policy === 'optional'}
        <Button variant="danger" onclick={() => manage('/api/auth/mfa/disable')} disabled={busy}>Disable MFA</Button>
      {/if}
    </div>
  {:else if status}
    <div class="flex max-w-xl flex-col gap-3 sm:flex-row">
      <Input
        bind:value={currentPassword}
        type="password"
        autocomplete="current-password"
        placeholder="Current password"
      />
      <Button onclick={startEnrollment} disabled={busy || !currentPassword}>
        Enable authenticator MFA
      </Button>
    </div>
  {/if}

  {#if error}
    <p class="rounded-2xl border border-rose-500/40 bg-rose-500/10 px-4 py-3 text-sm text-rose-200">{error}</p>
  {/if}
</div>
