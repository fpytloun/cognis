<script lang="ts" generics="T extends string">
  /** Compact labelled choice for toolbars and panel headers. */
  let { value, options, label, onchange, class: className = '' }: {
    value: T;
    options: { value: T; label: string }[];
    /** Accessible name. */
    label: string;
    onchange: (value: T) => void;
    class?: string;
  } = $props();
</script>

<select class={['min-h-[40px] rounded-xl border border-control-border bg-surface px-2 text-xs text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent md:min-h-[30px]', className]}
  aria-label={label} {value} onchange={(event) => onchange(event.currentTarget.value as T)}>
  {#each options as option (option.value)}<option value={option.value}>{option.label}</option>{/each}
</select>
