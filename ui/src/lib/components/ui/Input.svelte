<script lang="ts">
  import { cn } from '$lib/utils';

  let {
    type = 'text',
    value = $bindable(''),
    placeholder = '',
    class: className = '',
    disabled = false,
    name
  } = $props<{
    type?: string;
    value?: string | number;
    placeholder?: string;
    class?: string;
    disabled?: boolean;
    name?: string;
  }>();
</script>

<input
  class={cn(
    'w-full rounded-xl border border-slate-700 bg-slate-950/80 px-3 py-2 text-sm text-slate-100 placeholder:text-slate-500',
    className
  )}
  {type}
  {value}
  {placeholder}
  {disabled}
  {name}
/>
