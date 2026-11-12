<script lang="ts">
  import { page } from '$app/state';
  import { goto } from '$app/navigation';
  import UsageWorkspace from '$lib/usage/UsageWorkspace.svelte';
  import { getUsageHistory } from '$lib/usage/history.svelte';
  const history = getUsageHistory();
</script>
<svelte:head><title>Usage · Cognis</title></svelte:head>
<div class="mx-auto w-full max-w-screen-2xl min-w-0 p-4 md:p-6">
  <UsageWorkspace url={page.url} historyState={history.state} navigate={(url, state) => void goto(url, { noScroll: true, keepFocus: true, state: { ...page.state, ...state } })}/>
</div>
