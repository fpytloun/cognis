<script lang="ts">
  import { goto } from '$app/navigation';
  import { onMount } from 'svelte';

  import Button from '$lib/components/ui/Button.svelte';
  import Card from '$lib/components/ui/Card.svelte';
  import Input from '$lib/components/ui/Input.svelte';
  import { auth } from '$lib/stores/auth';

  let email = '';
  let password = '';
  let error = '';
  let submitting = false;

  onMount(() => {
    void auth.bootstrap().then(async () => {
      if (auth.getSnapshot().status === 'authenticated') {
        await goto('/chat', { replaceState: true });
      }
    });
  });

  async function handleSubmit(event: SubmitEvent): Promise<void> {
    event.preventDefault();
    error = '';
    submitting = true;

    try {
      await auth.login(email, password);
      await goto('/chat', { replaceState: true });
    } catch (caughtError) {
      error = caughtError instanceof Error ? caughtError.message : 'Unable to log in.';
    } finally {
      submitting = false;
    }
  }
</script>

<svelte:head>
  <title>Sign In · Cognis</title>
</svelte:head>

<div class="flex min-h-screen items-center justify-center px-4">
  <Card class="w-full max-w-sm p-8 sm:p-10">
    <div class="space-y-6">
      <div class="space-y-1 text-center">
        <h1 class="text-2xl font-semibold text-white">Cognis</h1>
        <p class="text-sm text-slate-400">User sign-in</p>
      </div>

      <form class="space-y-5" onsubmit={handleSubmit} novalidate>
        <label class="block space-y-2 text-sm font-medium text-slate-200">
          <span>Email</span>
          <Input bind:value={email} name="email" type="email" autocomplete="username" placeholder="admin@example.com" />
        </label>

        <label class="block space-y-2 text-sm font-medium text-slate-200">
          <span>Password</span>
          <Input bind:value={password} name="password" type="password" autocomplete="current-password" placeholder="••••••••" />
        </label>

        {#if error}
          <p class="rounded-2xl border border-rose-500/40 bg-rose-500/10 px-4 py-3 text-sm text-rose-200">
            {error}
          </p>
        {/if}

        <Button class="w-full justify-center" type="submit" disabled={submitting}>
          {submitting ? 'Signing in…' : 'Sign in'}
        </Button>
      </form>
    </div>
  </Card>
</div>
