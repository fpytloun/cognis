<script lang="ts">
  import type { HTMLInputAttributes } from 'svelte/elements';

  import { cn } from '$lib/utils';

  /**
   * Input primitive. Mobile font-size is 16px to prevent iOS Safari from zooming
   * the viewport on focus; desktop scales back down to 14px at md+. Minimum
   * height is 44px mobile, 36px at md+.
   */

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
    'w-full rounded-xl border border-[color:var(--theme-border)] bg-[color:var(--theme-bg)] px-3 text-base text-[color:var(--theme-text)] placeholder:text-[color:var(--theme-text-muted)] min-h-[44px] py-2.5 md:min-h-[36px] md:py-2 md:text-sm',
    className
  )}
  {type}
  bind:value
  {placeholder}
  {disabled}
  {name}
  {...rest}
/>
