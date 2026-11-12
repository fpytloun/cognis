<script lang="ts">
  import { page } from '$app/state';
  import AppContactChat from '$lib/components/AppContactChat.svelte';
</script>

<AppContactChat contactId={page.params.contactId ?? ''} standalone />
