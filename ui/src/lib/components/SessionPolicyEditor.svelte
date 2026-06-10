<script lang="ts">
  let {
    allowText = $bindable(''),
    denyText = $bindable(''),
    disabled = false,
    title = 'Session policies',
    help = 'Optional Intaris policies for this session. Enter one policy per line. Plain text is preferred; JSON objects are also accepted one per line.'
  } = $props<{
    allowText?: string;
    denyText?: string;
    disabled?: boolean;
    title?: string;
    help?: string;
  }>();
</script>

<div class="rounded-2xl border border-slate-800 bg-slate-950/40 p-4">
  <div class="mb-3">
    <p class="text-xs uppercase tracking-[0.25em] text-slate-400">{title}</p>
    <p class="mt-1 text-xs text-slate-500">{help}</p>
  </div>
  <div class="grid gap-3 md:grid-cols-2">
    <label class="space-y-2 text-sm font-medium text-slate-200">
      <span>Allow policies</span>
      <textarea
        bind:value={allowText}
        disabled={disabled}
        class="min-h-[96px] w-full rounded-2xl border border-emerald-900/60 bg-slate-950/80 px-4 py-3 text-sm text-slate-100 placeholder:text-slate-500 disabled:opacity-50"
        placeholder="Session is allowed to pass AWS SSO and grant CLI access"
      ></textarea>
      <span class="block text-xs text-slate-500">Scope expansions and explicitly allowed support actions.</span>
    </label>
    <label class="space-y-2 text-sm font-medium text-slate-200">
      <span>Deny policies</span>
      <textarea
        bind:value={denyText}
        disabled={disabled}
        class="min-h-[96px] w-full rounded-2xl border border-rose-900/60 bg-slate-950/80 px-4 py-3 text-sm text-slate-100 placeholder:text-slate-500 disabled:opacity-50"
        placeholder="Session must not make write operations on environments reached through AWS SSM"
      ></textarea>
      <span class="block text-xs text-slate-500">Hard restrictions. Deny policies win over allow policies.</span>
    </label>
  </div>
</div>
