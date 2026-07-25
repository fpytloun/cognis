<script lang="ts">
  import { renderInlineMarkdown, stripMarkdown } from '$lib/markdown';
  import { blockText, blockTitle, type RichBlock } from '$lib/rich-deliverable';
  import { agendaTime, normalizeDayAgenda } from '../day-agenda';
  export let block: RichBlock;
  $: agenda = normalizeDayAgenda(block);
</script>

<section
  class="rich-day-agenda"
  aria-label={stripMarkdown(blockTitle(block) || "Today's agenda")}
  data-rich-block-type="day_agenda"
>
  <header>
    <div>
      {#if blockText(block, 'eyebrow')}<p>{@html renderInlineMarkdown(blockText(block, 'eyebrow'))}</p>{/if}
      <h2>{@html renderInlineMarkdown(blockTitle(block) || "Today's agenda")}</h2>
    </div>
    <div class="rich-agenda-clock">
      {#if agenda.now}<time datetime={agenda.now.toISOString()}>{agendaTime(agenda.now, agenda.timezone)}</time>{/if}
      {#if agenda.timezone}<small>{agenda.timezone}</small>{/if}
    </div>
  </header>
  {#if agenda.allDay.length}
    <div class="rich-agenda-all-day"><strong>All day</strong>
      {#each agenda.allDay as item}<span>{@html renderInlineMarkdown(item.title)}</span>{/each}
    </div>
  {/if}
  {#if agenda.timed.length}
    <ol class="rich-agenda-timeline" aria-label="Schedule">
      {#each agenda.timed as item, index}
        {#if agenda.markerIndex === index && agenda.now}
          <li class="rich-agenda-now-marker" aria-label={`Aktuální čas ${agendaTime(agenda.now, agenda.timezone)}`}>
            <span></span><time datetime={agenda.now.toISOString()}>{agendaTime(agenda.now, agenda.timezone)}</time>
          </li>
        {/if}
        <li class:next={item.isNext} class:free={item.kind === 'free'} class:past={item.state === 'past'} class:current={item.state === 'current'}>
          <div class="rich-agenda-time">
            <time datetime={item.start?.toISOString()}>{agendaTime(item.start, agenda.timezone)}</time>
            {#if item.end}<time datetime={item.end.toISOString()}>{agendaTime(item.end, agenda.timezone)}</time>{/if}
          </div>
          <div>
            {#if item.isNext}<small>{item.state === 'current' ? 'Now' : 'Next'}</small>{/if}
            <strong>{@html renderInlineMarkdown(item.title)}</strong>
            {#if item.location}<span>{@html renderInlineMarkdown(item.location)}</span>{/if}
            {#if item.description}<p>{@html renderInlineMarkdown(item.description)}</p>{/if}
          </div>
        </li>
      {/each}
      {#if agenda.markerIndex === agenda.timed.length && agenda.now}
        <li class="rich-agenda-now-marker" aria-label={`Current time ${agendaTime(agenda.now, agenda.timezone)}`}>
          <span></span><time datetime={agenda.now.toISOString()}>{agendaTime(agenda.now, agenda.timezone)}</time>
        </li>
      {/if}
    </ol>
  {:else}
    <p class="rich-agenda-empty">No timed events scheduled today.</p>
  {/if}
  {#if agenda.tasks.length}
    <div class="rich-agenda-tasks" aria-label="Todoist úkoly">
      <strong>Tasks · {agenda.tasks.length}</strong>
      <ul>{#each agenda.tasks.slice(0, 4) as task}<li>{@html renderInlineMarkdown(task.title)}</li>{/each}</ul>
    </div>
  {/if}
  {#if agenda.source}
    <footer>
      {#if agenda.source.url}<a href={agenda.source.url} rel="noreferrer">{agenda.source.label || agenda.source.url}</a>{:else}{@html renderInlineMarkdown(agenda.source.label)}{/if}
      {#if agenda.source.refreshedAt} · updated {agenda.source.refreshedAt}{/if}
    </footer>
  {/if}
</section>
