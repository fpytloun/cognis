<script lang="ts">
  import Button from '$lib/components/ui/Button.svelte';
  import Input from '$lib/components/ui/Input.svelte';
  import type { SecretMetadata } from '$lib/types/api';
  import type { MCPEnvVar } from '$lib/agents';

  export let envVars: MCPEnvVar[] = [];
  export let secrets: SecretMetadata[] = [];
  export let onChange: (next: MCPEnvVar[]) => void;
  export let onCreateSecret: (key: string) => void;

  function updateAt(index: number, patch: Partial<MCPEnvVar>): void {
    const next = envVars.map((entry, current) => (current === index ? { ...entry, ...patch } : entry));
    onChange(next);
  }

  function removeAt(index: number): void {
    onChange(envVars.filter((_, current) => current !== index));
  }

  function addRow(): void {
    onChange([...envVars, { key: '', value: '', type: 'literal' }]);
  }
</script>

<div class="space-y-3">
  <div class="flex items-center justify-between gap-3">
    <span class="text-sm text-slate-200">Environment variables</span>
    <Button size="sm" variant="secondary" type="button" onclick={addRow}>Add variable</Button>
  </div>

  {#if envVars.length === 0}
    <div class="rounded-2xl border border-slate-800 bg-slate-950/40 px-4 py-3 text-sm text-slate-400">
      No environment variables configured.
    </div>
  {/if}

  {#each envVars as entry, index}
    <div class="rounded-2xl border border-slate-800 bg-slate-950/40 p-4 space-y-3">
      <div class="grid gap-3 md:grid-cols-[minmax(0,1fr)_160px]">
        <label class="space-y-1 text-sm text-slate-200">
          <span>Key</span>
          <Input value={entry.key} placeholder="GITHUB_TOKEN" oninput={(event) => updateAt(index, { key: (event.currentTarget as HTMLInputElement).value })} />
        </label>
        <label class="space-y-1 text-sm text-slate-200">
          <span>Source</span>
          <select bind:value={entry.type} class="w-full rounded-xl border border-slate-700 bg-slate-950/80 px-3 py-2 text-sm text-slate-100" onchange={() => updateAt(index, { type: entry.type, value: entry.type === 'secret' ? entry.value : entry.value })}>
            <option value="literal">Custom value</option>
            <option value="secret">Credential store</option>
          </select>
        </label>
      </div>

      {#if entry.type === 'secret'}
        <div class="flex gap-2">
          <select class="w-full rounded-xl border border-slate-700 bg-slate-950/80 px-3 py-2 text-sm text-slate-100" bind:value={entry.value} onchange={() => updateAt(index, { value: entry.value })}>
            <option value="">Select credential...</option>
            {#each secrets.filter((s) => s.scope === 'global' || s.scope === 'user') as secret}
              <option value={secret.name}>{secret.name}{secret.description ? ` - ${secret.description}` : ''}</option>
            {/each}
          </select>
          <Button size="sm" variant="secondary" type="button" onclick={() => onCreateSecret(entry.key)}>New</Button>
        </div>
      {:else}
        <label class="space-y-1 text-sm text-slate-200 block">
          <span>Value</span>
          <Input value={entry.value} placeholder="your-value" oninput={(event) => updateAt(index, { value: (event.currentTarget as HTMLInputElement).value })} />
        </label>
      {/if}

      <div class="flex justify-end">
        <Button size="sm" variant="ghost" type="button" onclick={() => removeAt(index)}>Remove</Button>
      </div>
    </div>
  {/each}
</div>
