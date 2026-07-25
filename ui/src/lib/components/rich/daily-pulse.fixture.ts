import type { RichDeliverableVisualScenario } from './rich-deliverable.fixture';

const sources = [
  { id: 'context', number: 1, title: 'Ranní kontext', url: 'https://example.org/pulse/context', publisher: 'Cognis fixture' },
  { id: 'news', number: 2, title: 'Dopravní přehled', url: 'https://example.org/pulse/news', publisher: 'Cognis fixture' },
  { id: 'world', number: 3, title: 'Tržní přehled', url: 'https://example.org/pulse/world', publisher: 'Cognis fixture' },
  { id: 'ai', number: 4, title: 'AI provozní přehled', url: 'https://example.org/pulse/ai', publisher: 'Cognis fixture' },
  { id: 'weather', number: 5, title: 'Lovosice po hodinách', url: 'https://example.org/pulse/weather', publisher: 'Cognis fixture' },
];

export const dailyPulseScenario: RichDeliverableVisualScenario = {
  id: 'daily-pulse-v2',
  title: 'Ranní Pulse v2',
  description: 'Integrated Pulse v2 acceptance fixture with media, point agenda, citations, accordions, and SVG chart.',
  content: 'Ranní Pulse v2 — přístupná textová alternativa.',
  payload: {
    metadata: {
      presentation: 'pulse',
      pulse_variant: 'daily',
      pulse_version: 2,
      eyebrow: 'Úterý · Lovosice',
      subtitle: '14. července 2026 · integrovaná vizuální akceptace',
      toc: false,
      references: { dedicated_page: false },
    },
    blocks: [
      {
        type: 'hero',
        eyebrow: 'Osobní intelligence · 08:20',
        title: 'Ranní Pulse',
        subtitle: 'Jeden klidný blok, jeden bod v kalendáři a počasí vhodné pro dopolední přesun.',
      },
      {
        type: 'dashboard',
        blocks: [
          { type: 'metric', icon: 'calendar', label: 'Agenda', value: '3 body', delta: 'Bodová událost v 09:00' },
          { type: 'metric', icon: 'info', label: 'Podmínky', value: '18 °C', delta: 'Suché ráno' },
          { type: 'metric', icon: 'activity', label: 'Trh', value: 'Stabilní', delta: 'Bez tlaku na změnu' },
          { type: 'metric', icon: 'check', label: 'Priorita', value: 'Focus', delta: 'Dokončit do 09:00' },
        ],
      },
      {
        type: 'day_agenda',
        eyebrow: 'Dnešní rytmus',
        title: 'Úterý 14. července',
        now: '2026-07-14T08:20:00+02:00',
        timezone: 'Europe/Prague',
        compact: true,
        items: [
          { start: '2026-07-14T07:30:00+02:00', end: '2026-07-14T08:45:00+02:00', title: 'Soustředěná práce', kind: 'free' },
          { start: '2026-07-14T09:00:00+02:00', end: '2026-07-14T09:00:00+02:00', title: 'Odeslat rozhodnutí', description: 'Bodová událost bez umělého konce.' },
          { start: '2026-07-14T10:30:00+02:00', end: '2026-07-14T11:15:00+02:00', title: 'Pochůzky', location: 'Lovosice' },
        ],
        tasks: [{ title: 'Potvrdit prioritu' }, { title: 'Zkontrolovat trasu' }],
      },
      {
        type: 'columns',
        blocks: [
          {
            // `visual` renders the media as a full-bleed background behind
            // the header/summary/body, an image-forward treatment suited
            // to the Pulse's single most important signal of the day.
            type: 'card',
            variant: 'visual',
            icon: 'activity',
            eyebrow: 'Hlavní signál',
            title: 'Nejcennější okno končí v 09:00',
            summary: 'Dokončit jednu rozhodovací věc před bodovou událostí.',
            content: 'Kalendář a podmínky ukazují stejné praktické okno. [Kontext](https://example.org/pulse/context).',
            citations: ['context'],
            href: 'https://example.org/pulse/context',
            media: {
              src: '/fixtures/daily-pulse-lovosice.svg',
              alt: 'Lovosice a České středohoří v ranním světle',
              credit: 'Cognis acceptance fixture · editorial media',
              source_url: 'https://example.org/pulse/context',
              width: 1600,
              height: 900,
            },
          },
          {
            type: 'stack',
            title: 'Dnes udělat',
            blocks: [
              { type: 'card', variant: 'action', icon: 'check', title: 'Uzavřít rozhodnutí', content: 'Neotevírat před 09:00 vedlejší fronty.' },
              { type: 'card', variant: 'status', icon: 'clock', title: 'Připravit přesun', content: 'Pochůzky držet v chladnějším dopoledni.' },
            ],
          },
        ],
      },
      {
        type: 'section',
        title: 'Vědět',
        blocks: [
          {
            type: 'accordion',
            title: 'Zprávy',
            items: [
              {
                type: 'card',
                variant: 'editorial',
                title: 'Dopravní omezení se mění',
                summary: 'Před delší cestou stačí ověřit jedinou trasu.',
                content: 'Praktický dopad je omezený na plánovaný přesun. [Zdroj](https://example.org/pulse/news).',
                citations: ['news'],
                source_id: 'news',
                url: 'https://example.org/pulse/news',
              },
              {
                type: 'card',
                variant: 'editorial',
                title: 'Trhy zůstávají klidné',
                summary: 'Žádný signál nevyžaduje okamžitou změnu plánu.',
                content: 'Pohyb je v běžném rozsahu. [Zdroj](https://example.org/pulse/world).',
                citations: ['world'],
                source_id: 'world',
                url: 'https://example.org/pulse/world',
              },
            ],
          },
          {
            type: 'accordion',
            title: 'AI',
            items: [
              {
                type: 'card',
                variant: 'editorial',
                icon: 'info',
                title: 'Guardraily přecházejí do provozu',
                summary: 'Audit a oprávnění jsou důležitější než další demo.',
                content: 'Relevantní změna patří do pracovního backlogu. [Zdroj](https://example.org/pulse/ai).',
                citations: ['ai'],
                source_id: 'ai',
                url: 'https://example.org/pulse/ai',
              },
            ],
          },
        ],
      },
      {
        type: 'section',
        title: 'Sledovat',
        blocks: [{
          type: 'chart',
          title: 'Lovosice po hodinách',
          description: 'Teplota roste od klidného rána k teplejšímu odpoledni.',
          spec_version: 'cognis.chart.v1',
          chart_type: 'line',
          series: [{
            id: 'temperature',
            label: 'Teplota',
            points: [
              { x: '07', y: 17 },
              { x: '09', y: 19 },
              { x: '12', y: 23 },
              { x: '15', y: 25 },
              { x: '18', y: 22 },
            ],
          }],
          x_axis: { type: 'category' },
          y_axis: { type: 'linear' },
          source: 'Lovosice po hodinách',
          source_url: 'https://example.org/pulse/weather',
          observed_at: '2026-07-14T08:00:00+02:00',
        }],
      },
      { type: 'callout', title: 'Dnešní kurz', content: 'Jedno dokončené rozhodnutí před 09:00, potom praktické věci.' },
      { type: 'source_list', title: 'Reference', numbered: true },
    ],
    sources,
    assets: [],
    datasets: [],
    exports: [],
  },
};
