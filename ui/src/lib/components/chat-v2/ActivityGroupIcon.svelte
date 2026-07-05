<script lang="ts">
  import Brain from 'lucide-svelte/icons/brain';
  import Database from 'lucide-svelte/icons/database';
  import FilePenLine from 'lucide-svelte/icons/file-pen-line';
  import Globe from 'lucide-svelte/icons/globe';
  import Monitor from 'lucide-svelte/icons/monitor';
  import Search from 'lucide-svelte/icons/search';
  import Send from 'lucide-svelte/icons/send';
  import SquareTerminal from 'lucide-svelte/icons/square-terminal';
  import Wrench from 'lucide-svelte/icons/wrench';

  import type { ActivityGroupIcon as ActivityGroupIconKind } from '$lib/chat-v2/tool-groups';

  let { icon, class: className = '' } = $props<{
    icon: ActivityGroupIconKind;
    class?: string;
  }>();
</script>

{#if icon === 'search'}
  <Search class={className} />
{:else if icon === 'terminal'}
  <SquareTerminal class={className} />
{:else if icon === 'edit'}
  <FilePenLine class={className} />
{:else if icon === 'delegate'}
  <Send class={className} />
{:else if icon === 'globe'}
  <Globe class={className} />
{:else if icon === 'browser'}
  <Monitor class={className} />
{:else if icon === 'database'}
  <Database class={className} />
{:else if icon === 'brain'}
  <Brain class={className} />
{:else}
  <Wrench class={className} />
{/if}
