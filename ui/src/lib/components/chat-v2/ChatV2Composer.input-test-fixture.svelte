<script lang="ts">
  import ChatV2Composer from './ChatV2Composer.svelte';
  let value = $state('');
  let observed = $state<string[]>([]);
</script>

<ChatV2Composer bind:value onInput={() => { observed = [...observed, value]; }}
  onSend={() => {}} onStop={() => {}} onFiles={() => {}} />
<output data-testid="observed-input">{JSON.stringify(observed)}</output>
