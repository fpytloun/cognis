<script lang="ts">
  import '../app.css';

  import type { Snippet } from 'svelte';

  let { children }: { children: Snippet } = $props();
</script>

<a class="skip-link" href="#main">Skip to content</a>
{@render children()}
