<script lang="ts" generics="T extends string">
  /** Single choice between a few peer options (view mode, grouping, range preset). */
  let { options, value, label, onchange }: {
    options: { value: T; label: string }[];
    /** Null when no option is active (for example a custom range). */
    value: T | null;
    label: string;
    onchange: (value: T) => void;
  } = $props();
</script>

<div class="inline-flex max-w-full flex-wrap rounded-xl border border-control-border bg-inset p-0.5" role="radiogroup" aria-label={label}>
  {#each options as option (option.value)}
    <button type="button" role="radio" aria-checked={value === option.value}
      class="min-h-[40px] rounded-[10px] px-3 text-xs font-medium text-secondary transition hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent aria-checked:bg-surface aria-checked:text-foreground aria-checked:shadow-card md:min-h-[30px]"
      onclick={() => onchange(option.value)}>{option.label}</button>
  {/each}
</div>
