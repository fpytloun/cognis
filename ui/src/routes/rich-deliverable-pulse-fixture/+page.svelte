<script lang="ts">
  import RichDeliverable from '$lib/components/rich/RichDeliverable.svelte';
  import { dailyPulseScenario } from '$lib/components/rich/daily-pulse.fixture';
</script>

<main data-testid="rich-deliverable-pulse-fixture">
  <RichDeliverable
    title={dailyPulseScenario.title}
    content={dailyPulseScenario.content}
    payload={dailyPulseScenario.payload}
    instanceId="pulse-v2-chat-fixture"
    surface="embedded"
  />
</main>

<style>
  main { width: min(100%, 76rem); margin: 0 auto; padding: 1rem; }
</style>
