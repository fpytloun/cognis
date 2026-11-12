<script lang="ts">
  import InboxList from '$lib/components/InboxList.svelte';
</script>

<svelte:head><title>Notifications · Cognis</title></svelte:head>
<div class="mx-auto w-full max-w-3xl space-y-4 px-4 py-6">
  <div><h1 class="text-2xl font-semibold text-strong">Your inbox</h1><p class="mt-1 text-sm text-muted">Agent messages, sharing, and account activity. Approvals remain in the dashboard’s attention queue.</p></div>
  <InboxList />
</div>
