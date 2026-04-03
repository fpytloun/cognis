<script lang="ts">
  import { onMount } from 'svelte';

  import { api, asApiError } from '$lib/api/client';
  import { deriveGettingStartedSteps, setGettingStartedDismissed } from '$lib/getting-started';
  import { auth } from '$lib/stores/auth';
  import LoadingState from '$lib/components/LoadingState.svelte';
  import Button from '$lib/components/ui/Button.svelte';
  import Card from '$lib/components/ui/Card.svelte';
  import type { SystemDiagnostics } from '$lib/types/api';

  const guideLinks = [
    ['Getting started', 'https://github.com/fpytloun/cognis/blob/main/docs/guide/getting-started.md'],
    ['Configuring providers', 'https://github.com/fpytloun/cognis/blob/main/docs/guide/configuring-providers.md'],
    ['Creating agents', 'https://github.com/fpytloun/cognis/blob/main/docs/guide/creating-agents.md'],
    ['Using chat', 'https://github.com/fpytloun/cognis/blob/main/docs/guide/using-chat.md'],
    ['Managing tasks', 'https://github.com/fpytloun/cognis/blob/main/docs/guide/managing-tasks.md'],
    ['Troubleshooting', 'https://github.com/fpytloun/cognis/blob/main/docs/guide/troubleshooting.md']
  ] as const;

  let loading = true;
  let error = '';
  let diagnostics: SystemDiagnostics | null = null;

  async function loadDiagnostics(): Promise<void> {
    loading = true;
    error = '';
    if (auth.getSnapshot().user?.role !== 'admin') {
      loading = false;
      return;
    }
    try {
      diagnostics = await api.system.diagnostics();
    } catch (caughtError) {
      error = asApiError(caughtError).message;
    } finally {
      loading = false;
    }
  }

  function dismissGuide(): void {
    setGettingStartedDismissed(true);
  }

  onMount(() => {
    void loadDiagnostics();
  });
</script>

<svelte:head>
  <title>Getting Started · Cognis</title>
</svelte:head>

{#if loading}
  <LoadingState label="Loading onboarding guide" description="Checking your first-run readiness and available configuration steps." />
{:else}
  <section class="space-y-5">
    <Card class="p-6">
      <div class="flex flex-wrap items-center justify-between gap-3">
        <div>
          <p class="text-sm uppercase tracking-[0.25em] text-sky-300">Getting started</p>
          <h1 class="mt-2 text-2xl font-semibold text-white">Set up Cognis end to end</h1>
          <p class="mt-3 max-w-3xl text-sm leading-6 text-slate-400">
            Use this checklist to verify companion services, add an LLM provider, create an agent, and start your first chat.
          </p>
        </div>
        <Button variant="secondary" onclick={dismissGuide}>Skip guide</Button>
      </div>
    </Card>

    {#if error}
      <p class="rounded-2xl border border-rose-500/30 bg-rose-500/10 px-4 py-3 text-sm text-rose-100">{error}</p>
    {/if}

    {#if !diagnostics && !error}
      <Card class="p-6">
        <p class="text-sm text-slate-400">
          System diagnostics are only available to administrators. Contact your admin for setup assistance.
        </p>
      </Card>
    {/if}

    {#if diagnostics}
      <div class="grid gap-4 md:grid-cols-2">
        {#each deriveGettingStartedSteps(diagnostics) as step}
          <a class="rounded-2xl border border-slate-800 bg-slate-950/70 p-5" href={step.href}>
            <div class="flex items-center justify-between gap-3">
              <p class="font-medium text-white">{step.label}</p>
              <span class={`rounded-full px-3 py-1 text-xs ${step.done ? 'bg-emerald-500/20 text-emerald-200' : 'bg-amber-500/20 text-amber-200'}`}>
                {step.done ? 'Done' : 'Pending'}
              </span>
            </div>
            <p class="mt-3 text-sm leading-6 text-slate-400">{step.description}</p>
          </a>
        {/each}
      </div>

      <Card class="p-5">
        <p class="text-xs uppercase tracking-[0.25em] text-slate-400">Documentation</p>
        <div class="mt-4 grid gap-3 md:grid-cols-2">
          {#each guideLinks as [label, href]}
            <a class="rounded-2xl border border-slate-800 bg-slate-950/70 px-4 py-3 text-sm text-slate-200" href={href} target="_blank" rel="noreferrer">
              {label}
            </a>
          {/each}
        </div>
      </Card>
    {/if}
  </section>
{/if}
