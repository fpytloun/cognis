<script lang="ts">
  import RichDeliverable from '$lib/components/rich/RichDeliverable.svelte';
  import { richDeliverableDataScenario } from '$lib/components/rich/rich-deliverable-data.fixture';
  import { dailyPulseScenario } from '$lib/components/rich/daily-pulse.fixture';
  import { richDeliverableVisualScenarios } from '$lib/components/rich/rich-deliverable.fixture';

  const scenarios = [...richDeliverableVisualScenarios, dailyPulseScenario, richDeliverableDataScenario];
  let selectedIndex = 0;
  $: scenario = scenarios[selectedIndex] ?? scenarios[0];
</script>

<svelte:head>
  <title>Rich Deliverable Fixture · Cognis</title>
</svelte:head>

<main class="fixture-page" data-testid="rich-deliverable-fixture-page">
  <section class="fixture-hero">
    <div>
      <span>Visual QA</span>
      <h1>Rich Deliverables</h1>
      <p>Real-world fixture scenarios for browser-polishing the renderer beyond Markdown.</p>
    </div>
    <div class="fixture-tabs" role="tablist" aria-label="Rich deliverable scenarios">
      {#each scenarios as item, index}
        <button
          type="button"
          role="tab"
          aria-selected={index === selectedIndex}
          class:active={index === selectedIndex}
          on:click={() => selectedIndex = index}
        >
          {item.title}
        </button>
      {/each}
    </div>
  </section>

  <section class="fixture-shell" data-testid="rich-deliverable-fixture" data-scenario={scenario.id}>
    <div class="scenario-meta">
      <span>{scenario.id}</span>
      <p>{scenario.description}</p>
    </div>
    {#key scenario.id}
      <RichDeliverable
        title={scenario.title}
        content={scenario.content}
         payload={scenario.payload}
         instanceId={`fixture-${scenario.id}`}
         standaloneUrl={`/rich-deliverable-fixture#${scenario.id}`}
         surface="embedded"
      />
    {/key}
  </section>
</main>

<style>
  .fixture-page {
    width: 100%;
    min-width: 0;
    max-width: 100%;
    min-height: 100%;
    overflow: visible;
    background:
      radial-gradient(circle at 12% 0%, rgb(56 189 248 / 0.18), transparent 28rem),
      radial-gradient(circle at 88% 10%, rgb(16 185 129 / 0.13), transparent 26rem),
      linear-gradient(180deg, #020617, #07111a 42%, #020617);
    padding: clamp(1rem, 3vw, 2.5rem);
  }

  /* This QA harness previously stayed dark regardless of theme, which made
     light-themed rich deliverables (bare/uncarded markdown, low-opacity
     panels) impossible to visually verify correctly. Follow the resolved
     theme like real embedding contexts (chat, standalone) do. */
  @media (prefers-color-scheme: light) {
    :global(:root:not([data-resolved-theme="dark"])) .fixture-page {
      background:
        radial-gradient(circle at 12% 0%, rgb(3 105 161 / 0.1), transparent 28rem),
        radial-gradient(circle at 88% 10%, rgb(5 150 105 / 0.08), transparent 26rem),
        linear-gradient(180deg, #f8fafc, #eef2f6 42%, #f8fafc);
    }
  }

  .fixture-hero,
  .fixture-shell {
    width: min(100%, 86rem);
    min-width: 0;
    max-width: 100%;
    margin: 0 auto;
  }

  .fixture-hero {
    display: grid;
    grid-template-columns: minmax(18rem, 1fr) minmax(20rem, 1.35fr);
    gap: 1.5rem;
    align-items: end;
    margin-bottom: 1.5rem;
  }

  .fixture-hero span,
  .scenario-meta span {
    color: rgb(125 211 252);
    font-size: 0.75rem;
    font-weight: 850;
    letter-spacing: 0.18em;
    text-transform: uppercase;
  }

  .fixture-hero h1 {
    margin: 0.25rem 0;
    color: rgb(248 250 252);
    font-size: clamp(2.4rem, 7vw, 6rem);
    letter-spacing: -0.07em;
    line-height: 0.88;
  }

  .fixture-hero p,
  .scenario-meta p {
    margin: 0;
    color: rgb(203 213 225);
    line-height: 1.6;
  }

  .fixture-tabs {
    display: flex;
    flex-wrap: wrap;
    justify-content: flex-end;
    gap: 0.55rem;
  }

  .fixture-tabs button {
    border: 1px solid rgb(148 163 184 / 0.16);
    border-radius: 999px;
    background: rgb(15 23 42 / 0.62);
    color: rgb(203 213 225);
    padding: 0.5rem 0.75rem;
    font-size: 0.78rem;
    font-weight: 750;
  }

  .fixture-tabs button.active,
  .fixture-tabs button:hover {
    border-color: rgb(56 189 248 / 0.45);
    background: rgb(14 165 233 / 0.16);
    color: rgb(248 250 252);
  }

  .fixture-shell {
    display: grid;
    gap: 1rem;
  }

  .scenario-meta {
    display: flex;
    flex-wrap: wrap;
    justify-content: space-between;
    gap: 0.75rem;
    border: 1px solid rgb(148 163 184 / 0.12);
    border-radius: 1.25rem;
    background: rgb(15 23 42 / 0.55);
    padding: 1rem 1.15rem;
  }

  @media (max-width: 860px) {
    .fixture-page {
      padding: 0.5rem;
    }

    .fixture-hero {
      grid-template-columns: 1fr;
    }

    .fixture-tabs {
      justify-content: flex-start;
    }
  }

  @media (prefers-color-scheme: light) {
    :global(:root:not([data-resolved-theme="dark"])) {
      .fixture-hero h1 { color: rgb(23 32 51); }
      .fixture-hero p,
      .scenario-meta p { color: rgb(51 65 85); }
      .fixture-tabs button {
        border-color: rgb(51 65 85 / 0.16);
        background: rgb(255 255 255 / 0.7);
        color: rgb(51 65 85);
      }
      .fixture-tabs button.active,
      .fixture-tabs button:hover {
        border-color: rgb(3 105 161 / 0.4);
        background: rgb(224 242 254);
        color: rgb(23 32 51);
      }
      .scenario-meta {
        border-color: rgb(51 65 85 / 0.14);
        background: rgb(255 255 255 / 0.65);
      }
    }
  }
</style>
