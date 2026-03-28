<script lang="ts">
  import { goto } from '$app/navigation';
  import { onMount } from 'svelte';

  import Button from '$lib/components/ui/Button.svelte';
  import Card from '$lib/components/ui/Card.svelte';
  import Input from '$lib/components/ui/Input.svelte';
  import { apiUrl } from '$lib/config';
  import { auth } from '$lib/stores/auth';

  let loading = true;
  let submitting = false;
  let token = '';
  let setupAvailable = true;
  let setupComplete = false;
  let error = '';
  let form = {
    email: '',
    name: '',
    password: '',
    confirmPassword: ''
  };

  function validate(): string | null {
    if (!token) {
      return 'The setup token is missing from the URL.';
    }
    if (!form.email.includes('@')) {
      return 'Enter a valid email address.';
    }
    if (form.password.length < 8) {
      return 'Password must be at least 8 characters long.';
    }
    if (form.password !== form.confirmPassword) {
      return 'Password confirmation must match.';
    }
    return null;
  }

  async function loadStatus(): Promise<void> {
    const url = new URL(window.location.href);
    token = url.searchParams.get('token') ?? '';
    const status = await fetch(apiUrl('/api/bootstrap-status'));
    const payload = await status.json();
    setupAvailable = Boolean(payload.setup_available);
    setupComplete = Boolean(payload.setup_complete);
  }

  async function handleSubmit(event: SubmitEvent): Promise<void> {
    event.preventDefault();
    error = '';
    const validationError = validate();
    if (validationError) {
      error = validationError;
      return;
    }

    submitting = true;
    try {
      const response = await fetch(apiUrl('/api/setup'), {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json'
        },
        body: JSON.stringify({
          token,
          email: form.email,
          name: form.name || null,
          password: form.password
        })
      });

      if (!response.ok) {
        if (response.status === 401) {
          throw new Error('The setup token is invalid or expired. Restart Cognis or use the local CLI to create the first admin user.');
        }
        if (response.status === 404) {
          throw new Error('Setup has already been completed. Open the login page to continue.');
        }
        throw new Error('Unable to complete setup.');
      }

      await auth.login(form.email, form.password);
      await goto('/chat', { replaceState: true });
    } catch (caughtError) {
      error = caughtError instanceof Error ? caughtError.message : 'Unable to complete setup.';
    } finally {
      submitting = false;
    }
  }

  onMount(() => {
    void loadStatus()
      .catch((caughtError) => {
        error = caughtError instanceof Error ? caughtError.message : 'Unable to load setup status.';
      })
      .finally(() => {
        loading = false;
      });
  });
</script>

<svelte:head>
  <title>Setup · Cognis</title>
</svelte:head>

<div class="mx-auto flex min-h-screen max-w-5xl items-center justify-center px-6 py-16">
  <Card class="w-full max-w-2xl p-8">
    {#if loading}
      <p class="text-sm text-slate-300">Loading setup status…</p>
    {:else if setupComplete && !setupAvailable}
      <div class="space-y-4">
        <p class="text-sm uppercase tracking-[0.3em] text-sky-300">Setup complete</p>
        <h1 class="text-3xl font-semibold text-white">Cognis is already configured</h1>
        <p class="text-sm leading-6 text-slate-400">Open the login page to sign in with an existing account.</p>
        <Button onclick={() => goto('/login')}>Go to login</Button>
      </div>
    {:else}
      <div class="space-y-6">
        <div>
          <p class="text-sm uppercase tracking-[0.3em] text-sky-300">First run</p>
          <h1 class="mt-3 text-3xl font-semibold text-white">Create the first admin account</h1>
          <p class="mt-3 text-sm leading-6 text-slate-400">
            This creates the initial administrator for your Cognis instance. If the setup token expired, restart Cognis or run <code>cognis admin create-user</code> locally.
          </p>
        </div>

        <form class="space-y-4" onsubmit={handleSubmit}>
          <label class="block space-y-2 text-sm font-medium text-slate-200">
            <span>Email</span>
            <Input bind:value={form.email} type="email" placeholder="admin@example.com" />
          </label>
          <label class="block space-y-2 text-sm font-medium text-slate-200">
            <span>Name</span>
            <Input bind:value={form.name} placeholder="Admin" />
          </label>
          <label class="block space-y-2 text-sm font-medium text-slate-200">
            <span>Password</span>
            <Input bind:value={form.password} type="password" placeholder="••••••••" />
          </label>
          <label class="block space-y-2 text-sm font-medium text-slate-200">
            <span>Confirm password</span>
            <Input bind:value={form.confirmPassword} type="password" placeholder="••••••••" />
          </label>

          {#if error}
            <p class="rounded-2xl border border-rose-500/30 bg-rose-500/10 px-4 py-3 text-sm text-rose-100">{error}</p>
          {/if}

          <Button class="w-full justify-center" type="submit" disabled={submitting}>
            {submitting ? 'Creating account…' : 'Complete setup'}
          </Button>
        </form>
      </div>
    {/if}
  </Card>
</div>
