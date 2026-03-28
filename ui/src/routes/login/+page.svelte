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
  <title>Log In · Cognis</title>
</svelte:head>

<div class="mx-auto flex min-h-screen max-w-6xl items-center justify-center px-6 py-16">
  <Card class="grid w-full max-w-5xl overflow-hidden lg:grid-cols-[1.2fr_0.8fr]">
    <section class="hidden border-r border-slate-800/80 bg-slate-950/80 p-10 lg:block">
      <p class="text-sm font-medium uppercase tracking-[0.3em] text-sky-300">Cognis</p>
      <h1 class="mt-6 text-4xl font-semibold leading-tight text-white">
        Decoupled control plane for safe, streaming agent workflows.
      </h1>
      <p class="mt-6 max-w-xl text-sm leading-7 text-slate-300">
        Log in to manage agents, watch delegated work progress in real time, review workflow gates,
        and configure the Openclaw controller from one browser workspace.
      </p>
    </section>

    <section class="p-8 sm:p-10">
      <div class="mx-auto max-w-md space-y-8">
        <div class="space-y-3">
          <p class="text-sm font-medium uppercase tracking-[0.3em] text-sky-300">Sign in</p>
          <h2 class="text-3xl font-semibold text-white">Welcome back</h2>
          <p class="text-sm leading-6 text-slate-400">
            Use your Cognis account to open the UI, fetch your agent workspace, and start a secure
            WebSocket session.
          </p>
        </div>

        <form class="space-y-5" onsubmit={handleSubmit}>
          <label class="block space-y-2 text-sm font-medium text-slate-200">
            <span>Email</span>
            <Input bind:value={email} name="email" type="email" placeholder="admin@example.com" />
          </label>

          <label class="block space-y-2 text-sm font-medium text-slate-200">
            <span>Password</span>
            <Input bind:value={password} name="password" type="password" placeholder="••••••••" />
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
    </section>
  </Card>
</div>
