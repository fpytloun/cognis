<script lang="ts">
  import type { HTMLInputAttributes } from 'svelte/elements';

  import { cn } from '$lib/utils';

  let {
    type = 'text',
    value = $bindable(''),
    placeholder = '',
    class: className = '',
    disabled = false,
    name,
    ...rest
  } = $props<{
    type?: string;
    value?: string | number;
    placeholder?: string;
    class?: string;
    disabled?: boolean;
    name?: string;
  } & HTMLInputAttributes>();
</script>

<input
  class={cn(
    'w-full rounded-xl border border-slate-700 bg-slate-950/80 px-3 py-2 text-sm text-slate-100 placeholder:text-slate-500',
    className
  )}
  {type}
  bind:value
  {placeholder}
  {disabled}
  {name}
  {...rest}
/>
