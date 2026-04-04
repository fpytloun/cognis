<script lang="ts">
  import ShieldCheck from 'lucide-svelte/icons/shield-check';

  import Button from '$lib/components/ui/Button.svelte';
  import Card from '$lib/components/ui/Card.svelte';
  import Input from '$lib/components/ui/Input.svelte';
  import type { ChannelContact, ChannelMeta } from '$lib/types/api';

  export let channelTypes: ChannelMeta[] = [];
  export let contacts: ChannelContact[] = [];
  export let contactForm: { channel_type: string; sender_id: string; display_name: string };
  export let busy = false;
  export let onSave: () => void;
</script>

<div class="grid gap-6 xl:grid-cols-[0.85fr_1.15fr]">
  <Card class="p-5">
    <p class="text-xs uppercase tracking-[0.24em] text-slate-500">Verified senders</p>
    <h2 class="mt-1 text-lg font-semibold text-white">Manual override</h2>
    <p class="mt-1 text-sm text-slate-400">Use this advanced flow only when you already know the external sender ID and want to bypass pairing.</p>

    <div class="mt-5 grid gap-4">
      <label class="grid gap-2 text-sm text-slate-300">
        Channel type
        <select bind:value={contactForm.channel_type} class="w-full rounded-xl border border-slate-700 bg-slate-950/80 px-3 py-2 text-sm text-slate-100">
          {#each channelTypes as meta}
            <option value={meta.channel_type}>{meta.label}</option>
          {/each}
        </select>
      </label>
      <label class="grid gap-2 text-sm text-slate-300">
        Sender ID
        <Input bind:value={contactForm.sender_id} placeholder="Phone number, platform user ID, chat user handle, ..." />
      </label>
      <label class="grid gap-2 text-sm text-slate-300">
        Display name
        <Input bind:value={contactForm.display_name} placeholder="Optional friendly label" />
      </label>
      <div class="flex justify-end">
        <Button variant="primary" onclick={onSave} disabled={busy}>
          <ShieldCheck class="mr-2 h-4 w-4" /> Save sender
        </Button>
      </div>
    </div>
  </Card>

  <div class="space-y-4">
    {#if contacts.length === 0}
      <Card class="p-6 text-sm text-slate-300">No verified senders yet. Pair a remote sender or add one manually.</Card>
    {/if}
    {#each contacts as contact (contact.contact_id)}
      <Card class="p-5">
        <div class="flex flex-col gap-2 lg:flex-row lg:items-center lg:justify-between">
          <div>
            <div class="flex flex-wrap items-center gap-2">
              <h2 class="text-lg font-semibold text-white">{contact.display_name || contact.sender_id}</h2>
              <span class="rounded-full border border-emerald-500/20 bg-emerald-500/10 px-3 py-1 text-xs text-emerald-300">verified</span>
              <span class="rounded-full border border-slate-700 px-3 py-1 text-xs text-slate-300">{contact.channel_type}</span>
            </div>
            <p class="mt-2 text-sm text-slate-400">Sender ID: {contact.sender_id}</p>
            <p class="text-sm text-slate-400">Linked to your Cognis account</p>
          </div>
        </div>
      </Card>
    {/each}
  </div>
</div>
