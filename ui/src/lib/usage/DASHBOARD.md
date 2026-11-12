# Usage dashboard contract

Mode: **Analyze**. Understand, attribute and control LLM consumption and its
estimated cost.

## Audience and moment of use

The workspace owner (My usage) or an administrator (All users) during a daily
or weekly review: "What did inference cost, what drove it, and is anything
about to run out or going unpriced?" A project member sees project-visible
usage only.

## Overview: one question, six numbers

| Number | Definition | Unit | Baseline | Broken / attention rule | First action |
|---|---|---|---|---|---|
| Estimated cost | Known priced subtotal of the selected currency/basis band | currency | Average per elapsed day of the range | Any unpriced attempt (see below) | Open the spend breakdown |
| Tokens | Fresh input, cached input and output tokens | tokens | Share of each part | Unknown usage > 0 | Open Costs & Tokens |
| Requests | Finalized attempts | count | Failed count and failure ratio | Failure ratio ≥ 5 % (amber), ≥ 20 % (red) | Open failed requests |
| Unpriced | Attempts without a price for any token dimension | count | — (problem count) | > 0 draws attention | Set a model price or refresh the bundled catalog |
| Cache reads | Cached input / all input | % | — | none | — |
| Subscription limits | Used % per provider window, reset time | % | Window limit | ≥ 70 % amber, ≥ 90 % red | Switch provider or wait for the reset |

Money is always an **estimate** (API-equivalent or configured price), never an
invoice. Unpriced and partially priced usage stays a visible count, never zero.
Missing values show an em dash, never zero.

## Data sources

| Source | API | Refresh | Lag | Retention | No data means |
|---|---|---|---|---|---|
| Aggregates, trend, breakdowns | `POST /api/v1/usage/query` (overview, trend, cost_breakdowns) | Page load, refresh button, usage invalidation events | Projection lag (seconds) | Hourly/daily totals; request detail per retention policy | Unknown, not zero |
| Requests and sessions | `POST /api/v1/usage/attempts/search`, `/requests`, `/sessions` | On demand | Same | Detail retention cutoff | Detail expired |
| Subscription limits | `GET /api/v1/provider-monitor` + `GET /api/v1/usage/providers/{id}/history` | Page load | Provider poll interval (~15 min) | 90-day query window | No subscription window |

## Scale, time and scope

- Default range: the last 30 days (UTC dates). Presets: Today, 7 days, 30 days,
  this month. Custom dates stay available.
- Granularity: hourly buckets for ranges up to three days, daily buckets beyond
  (`bucketFor` in `query-contract.ts`). The current bucket is drawn as partial.
  Per-unit baselines follow the same unit and divide only the elapsed time.

## Views

| View | Question | Main encodings |
|---|---|---|
| Overview | What did it cost, what drove it, is anything near a limit? | KPI strip, cost/token bars, ranked breakdowns, limit bars |
| Costs & tokens | Where does spend and token volume go over time? | Cost bars, token stacks, cache share line, sortable cost table |
| Performance | Are requests fast and reliable? | Pooled KPIs, latency/speed/failure lines, sortable group table |
| Requests | Which sessions, requests and attempts make it up? | Tables with drill-through and attempt detail |
| Services | How much do companion services use? | Service table, per-service request bars |
| Limit history | How close am I to subscription limits over time? | Per-window step charts with 70/90 % thresholds |

Averages are pooled (sum over count), never means of bucket means.
- Time zone: UTC, labelled.
- Expected scale: tens to thousands of attempts per day, tens of models,
  a handful of providers and agents.

## Permissions and mutations

The dashboard is read-only. Scope (My / All users / Project) is enforced by the
server; the selector only requests a scope. Drill-downs keep scope, filters
and the exact interval. There are no mutating operations on this page.

## Known gaps

- No previous-period comparison: the API returns one interval per query.
  The cost baseline is the per-day average instead.
- No budget or forecast range is shown on Overview; the API forecast is a
  single observed-rate projection and stays on Costs & Tokens.
