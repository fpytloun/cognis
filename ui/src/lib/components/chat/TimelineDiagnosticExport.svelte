<script lang="ts">
  import type { ChatV2ClientState } from '$lib/chat-v2/sync-engine';
  import type { TimelineItem } from '$lib/chat-v2/types';

  let { clientState, visibleItems }: { clientState: ChatV2ClientState; visibleItems: TimelineItem[] } = $props();
  const diagnosticPanel = import('./TimelineDiagnosticExportPanel.svelte');
</script>

{#await diagnosticPanel}
  <p role="status" class="mt-4 text-xs text-quiet">Loading chat diagnostics…</p>
{:then { default: DiagnosticPanel }}
  <DiagnosticPanel {clientState} {visibleItems} />
{:catch}
  <p role="alert" class="mt-4 text-xs text-danger">Chat diagnostics could not load. Reload the app to try again.</p>
{/await}
