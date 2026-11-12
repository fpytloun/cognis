<script lang="ts">
  import { renderTerminalOutput } from '$lib/syntax/terminal-output';

  let { text } = $props<{ text: string }>();
</script>

{@html renderTerminalOutput(text)}
