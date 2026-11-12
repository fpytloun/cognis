<script lang="ts">
  import { onMount, tick } from 'svelte';
  import { findTarget, installFind } from '$lib/find';
  import Input from './ui/Input.svelte';
  import ChatSearchBar from './ChatSearchBar.svelte';
  import Sheet from './ui/Sheet.svelte';
  import BlockingDialog from './ui/BlockingDialog.svelte';

  let { submit }: { submit: () => void } = $props();
  let query = $state('retained query');
  let input = $state<HTMLInputElement | null>(null);
  let open = $state(false);
  let modal = $state(false);
  let modalSearch = $state(false);
  let tab = $state(false);
  let lower = $state(false);
  let upper = $state(false);
  onMount(installFind);
</script>

<button use:findTarget={{ primary: true, open: async () => { open = true; await tick(); return input; } }}>Conversation search</button>
<textarea aria-label="Composer">draft retained</textarea>
{#if open}
  <ChatSearchBar bind:query bind:inputElement={input} onSubmit={submit} onClose={() => open = false}
    onNext={() => {}} onPrevious={() => {}} />
{/if}
<button onclick={() => modal = true}>Open modal</button>
<button onclick={() => tab = !tab}>Change tab</button>
<button onclick={() => lower = true}>Open lower dialog</button>
<BlockingDialog open={lower} onClose={() => lower = false} label="Lower dialog">
  <Input find aria-label="Lower search" value="lower" />
  <button onclick={() => upper = true}>Open upper dialog</button>
</BlockingDialog>
<BlockingDialog open={upper} onClose={() => upper = false} label="Upper dialog">
  <Input find aria-label="Upper search" value="upper" />
</BlockingDialog>
<div hidden={!tab}><Input find aria-label="Other tab" value="other" /></div>
<Sheet open={modal} onClose={() => modal = false} label="Find test modal">
  <button onclick={() => modalSearch = !modalSearch}>Toggle modal search</button>
  {#if modalSearch}<Input find aria-label="Modal search" value="modal query" />{/if}
</Sheet>
