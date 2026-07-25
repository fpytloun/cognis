<script lang="ts">
  import type { RichBlock } from '$lib/rich-deliverable';

  export let block: RichBlock;
  export let type = 'unknown';
</script>

<details class="rich-unsupported" data-rich-block-type="unsupported">
  <summary>Unsupported block: {type}</summary>
  <pre class="rich-code">{JSON.stringify(block, null, 2)}</pre>
</details>
