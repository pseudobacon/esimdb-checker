# Architecture, data model & provenance

`esimdb-checker` is a read-only research CLI. This document covers the observed data
source, the module layout, the data model, the recommendation pipeline, and how every
report is made reproducible from cached snapshots.

## 1. Data source

esimdb.com is a Nuxt application. Its page DOM is virtualized (only a few hundred plan
rows render, in a non-exhaustive order), so DOM scraping would miss most of the catalog.
Instead this tool calls the **structured JSON endpoints the site itself fetches**:

| Endpoint | Purpose |
| --- | --- |
| `GET /api/client/countries` | all destinations: `code` (ISO), `name`, `slug`, `region`, `subregions`, `popularity` |
| `GET /api/client/regions` | regions: `slug`, `name`, `countries[]` (of `code`), `searches[]` |
| `GET /api/client/countries/<slug>/data-plans/folded?locale=en` | the plan catalog for a destination |

The folded plan payload is compact and **positional**:

```jsonc
{
  "plans": {
    "families": [ {            // shared feature bundles, referenced by index
      "provider": "<providerId>", "speedLimit": 5000, "reducedSpeed": 50,
      "possibleThrottling": false, "tethering": null, "has5G": true,
      "isLowLatency": false, "subscription": false, "subscription_period": null,
      "networks": ["..."], "hasAds": false, "payAsYouGo": false, "newUserOnly": false
    }, /* ... */ ],
    "rows": [                  // one plan per row
      [ <familyIdx>, <planId>, <capacityMB>, <periodDays>, {USD: 5.41, EUR: 5.46, ...},
        null, <name>, <dataCapPer?>, <notes?> ]
    ]
  },
  "providers": { "<providerId>": { "name": "...", "slug": "...", "popularity": 123 }, ... }
}
```

Notes:
- `capacityMB == 0` means **unlimited** data.
- `dataCapPer` (row index 7, when present and `day`/`month`/`week`) means the capacity is a
  **per-day** (or per-month/week) allowance, not a one-time allotment.
- `speedLimit` / `reducedSpeed` are in **kbps** (e.g. `5000` = 5 Mbps, matching the plan
  names on the site).
- `prices` is a per-currency dict with **distinct** amounts per currency (the site lists
  USD as the primary currency plus EUR/GBP/JPY/… as available).
- Rows reference a family by index; the family carries `provider` (a provider id) which
  resolves to `providers[id]` for the display name, slug, and popularity.

**Why this is acceptable for a research tool:** these are the same structured responses a
normal browser session receives; the tool only issues read-only GETs and never interacts
with checkout. This choice is documented per the task guidance to prefer observed public
structured responses over DOM scraping.

## 2. Module layout

```
src/esimdb_checker/
  config.py     base URL, endpoints, timeouts, retries, FX endpoint, default data dir
  http.py       httpx client builder + fetch-with-cache (raw bytes -> snapshot)
  snapshots.py  cache paths, read/write cache, SHA-256 manifest
  geo.py        Country/region loading + name/code/slug resolution + region membership
  catalog.py    parse the folded payload into Plan objects
  models.py     dataclasses: Country, Plan, Leg, Trip, Fx, Strategy, StrategyPlan, Report
  itinerary.py  parse --stop specs into a per-leg Trip (durations, transfer legs, data needs)
  coverage.py   plan-name scope resolution (label + ambiguity flags)
  filters.py    speed-cap detection (always-throttled vs after-allowance),
                purchasability, data-need check
  fx.py         FX conversion (Frankfurter, cached) + the supported-currency set + validation
  recommend.py  RecommendationEngine: fetch -> per-leg plans -> strategy families -> Report
  report.py     console / markdown / JSON renderers
  wizard.py     interactive wizard: shared WizardState + plain driver + Textual TUI + dispatch
  cli.py        argparse CLI (recommend, recommend-wizard, plans, countries, resolve, fetch)
```

### Presentation and input layers

Three separate jobs, so a currency decision is made in exactly one place:

| Concern | Lives in | Used by |
| --- | --- | --- |
| Which currencies exist / are convertible | `fx.SUPPORTED_CURRENCIES`, `fx.validate_currency_code` | wizard (both UIs), CLI flags (`argparse` type), `itinerary` load |
| Building/validating the itinerary (legs, transfer days, totals) | `itinerary.build_trip`, `itinerary.load_itinerary_*` | `recommend`, `recommend-wizard`, saved files |
| Turning a `Trip` into a report | `recommend.RecommendationEngine`, `report.render_*` | `recommend`, `recommend-wizard` |

`wizard.py` holds one front-end-independent `WizardState` (legs + currency + budget +
daily-data level, plus the validation each UI calls). The Textual app and the plain
line-oriented driver both only read/write that state and then call `build_trip()` /
`save_itinerary_file()`, so the two interviews cannot drift apart. `wants_tui()` is the
single predicate that decides which front-end runs (it requires all three streams to be
screens, no `--json`, and no `--plain`); the CLI routes the interview to **stderr** whenever
stdout must stay machine-readable.

`cmd_wizard` hands the interface a `search(trip, progress)` closure — the wizard owns the
question and the query, the CLI owns the rendering. The engine runs in a Textual worker
thread so the alternate screen stays up while esimdb.com is being queried, with
`RecommendationEngine.load(trip, progress)` reporting which destination it is fetching.
The TUI still never renders the report: it exits first, so the report is printed once on
the normal screen and remains visible in scrollback. `WizardResult` carries the `report` (so
`cmd_wizard` must not query twice) or a `search_error` (reported as a normal CLI error, never
a traceback). Without a `search` callback — the plain front-end, and the TUI's fallback — the
CLI runs the engine itself after the interview.

An exception inside the TUI cannot leave the terminal grabbed — Textual unwinds its alternate
screen context, `run_tui_wizard` swallows `KeyboardInterrupt`/`EOFError` into a cancelled
result, and `cmd_wizard` catches anything else and falls back to the plain interview. Keys are
swallowed while searching (only `Ctrl+C`, a priority binding, gets through to interrupt).

Two intentional behaviour differences between the front-ends, so the comparison is explicit
rather than accidental drift:

- **Review is read-only and searches on `Enter` with no confirmation page in the TUI**, while
  the plain interview still asks `Search this itinerary? (Y/n)`. The TUI can afford to be
  one keystroke shorter because `Esc` walks back to any earlier answer and the query runs
  visibly; the plain interview has no way back, so its last line is the only checkpoint it
  has.
- **Only the TUI runs the query itself.** The plain front-end leaves it to `cmd_wizard`
  (one `Searching esimdb.com for this itinerary ...` line), because a line-oriented
  interview has no screen to hold.

Three rendering/input rules keep the TUI usable; all were found by driving it in a real
pseudo-terminal and are invisible to a widget-level test:

- Any string containing literal `[...]` (key names in status/help/body lines) goes to
  `Static.update()` as a `rich.text.Text`, never a bare `str` — as a `str`, Rich parses the
  brackets as markup tags and the hint silently vanishes from the screen.
- The single answer field is shown **only while a question is open**. Keeping it on screen
  "just in case" (disabling it instead) put an empty box carrying the previous question's
  placeholder — literally `budget amount` — on the Review and data-level screens, beside a
  status line echoing the last answer, all of which the body already states. Hiding it is
  safe because of the two rules below.
- Key routing is decided by the app's own mode (`_typing()`), never by which widget happens
  to have focus. An `Input` that is un-hidden and re-focused *inside the same input batch*
  never receives the rest of that batch, so the first characters typed after `a`/`e`/`t`
  would vanish; `on_key` applies them instead (`insert_text_at_cursor`,
  `action_delete_left`, or submitting) when the field has not taken focus yet. Verified by
  sending `a` + `France` + Enter as a single pty write: the leg is added with 7 days.
- `Input.value` is assigned only when it differs from what is on screen. Assigning it on
  every repaint races the keystrokes still being dispatched, and typed text disappears.

Data flow for `recommend`:

```
CLI (args) -> parse_stops / build_trip -> Trip (legs with per-destination durations)
  -> Geo.load (countries + regions, cached)
  -> fx.fetch_fx (Frankfurter rate, cached)
  -> for each leg's slug: fetch_country_plans (folded, cached) -> Plan[]
  -> plan coverage = the set of leg pages each plan is listed on
  -> drop always-throttled + unpurchasable (+ optional provider filter)
  -> local picks: cheapest data-feasible, valid plan per leg
  -> regional candidates: cheapest data-feasible, valid plan per coverage scope
  -> build 3 families: local-only, whole-trip, hybrid
  -> recommend = cheapest feasible family (in --currency); gate on --budget
  -> Report (strategies, recommended, budget_status, diagnostics, totals, provenance)
  -> render (console / json / markdown)
```

**Each excluded plan is classified exactly once**, by the constraint that rules it out even
if the *next* filter were relaxed: provider filter → purchasability → data need → speed cap.
Only the last of those is enumerated, and only its always-throttled half. A plan below the
trip's data need was never a candidate for that trip, so it is counted
(`filtered before ranking…`) and not listed; a plan that met the data need and was removed
**only** because it is throttled from the first byte is listed in full, because that is the
decision worth reviewing — those are exactly the plans `--include-capped` could admit.
(Order matters here: a `1 Day / Daily 500MB` plan on a 1.5 GB/day trip is also capped, and
classifying it under the cap made the exclusions look like viable candidates the strict
filter was hiding.) Plans throttled only after their allowance are not exclusions at all —
they are candidates, counted in `after_cap_admitted` (or `after_cap_excluded_strict` under
`--strict-speed-cap`) so a run where they did the work is distinguishable from one where
none existed.

Detail rows are capped (`_DETAIL_CAP`), but `*_total` keys carry the real counts and both the
diagnostics and `totals` print those — a list length must never be presented as a total (the
pre-fix report said "speed-capped excluded: 50" when the true figure for a 5-destination
trip was 12,562, and `totals.speed_cap_excluded` repeated the same truncated number).

## 3. Data model (`models.py`)

- **Country** — `code` (ISO, e.g. `ES`), `name`, `slug`, `region`, `subregions`,
  `popularity`; `.key` (uppercased name) is the canonical identity used to match legs.
- **Plan** — `id`, `name`, `provider`, `provider_slug`, `capacity_mb` (0 = unlimited),
  `period_days` (0 = unknown), `provider_popularity`, `prices` (per-currency dict),
  `data_cap_per` (`"day"`/`"month"`/`"week"` — a daily cap means `capacity_mb` is the
  **per-day** amount, so the usable total is `capacity_mb × period_days`),
  `speed_limit`/`reduced_speed` (kbps), `possible_throttling`, `tethering`, `has_5g`,
  `is_low_latency`, `subscription`/`subscription_period`, `networks`, `has_ads`,
  `pay_as_you_go`, `new_user_only`, `notes`, `source_slug` (the destination page the plan
  was listed on).
  Derived: `usd_price`, `price_in(currency, fx)` (convert to a currency via FX),
  `price_and_currency()`, `unlimited`, `capacity_label()`, `period_label()`,
  `data_cap_label()`, `price_per_gb_usd()`, `effective_total_mb(days)`.
- **Leg** — one itinerary segment: `country` (None for a transfer leg), `days` (0 for a
  transfer leg), `transfer_days` (days before this leg; uncharged unless `Trip.transfer_data`
  > 0), `start_day`, `end_day`, `required_gb` (`days × daily_data_gb`, plus charged flying
  days), `country_name`, `is_transfer`.
- **Trip** — the per-destination itinerary: `legs` (ordered), `total_days` (charged days),
  `required_gb`, `daily_data_gb`, `transfer_data` (fraction 0..1 of the daily need a
  transfer/flying day uses; 0 = no data on transfer days), `currency`, `budget`
  (None = no budget), `start_date`, `end_date`; `.distinct_countries` is the ordered unique
  destination set. With `transfer_data > 0`, a leg's pre-days are charged into its own
  `required_gb`; a 0-day leg's share is charged to the next destination leg (whose plan
  activates on those days) or, at the trip's tail, to the previous leg, which must then
  stay valid through the layover.
  `total_days` sums **charged** days only; `.span_days` is the calendar length
  (charged + transfer days) and is what the report prints as the travel-day range,
  because a plan's validity has to survive the calendar rather than the charge.
- **Fx** — the converted rate: `available`, `currency`, `rate` (1 USD → rate units),
  `source` (`frankfurter`/`fallback`), `fetched_at`, `date`, `stale`, `note`;
  `convert(amount_usd)`.
- **Strategy** — a complete purchase plan for the whole trip: `family`
  (`local_only`/`whole_trip`/`hybrid`), `plans` (tuple of `StrategyPlan`), `total_cost`
  (in `currency`), `comparable`, `meets_data`, `validity_ok`, `assumptions`;
  `.purchase_count`.
- **StrategyPlan** — one purchased plan within a strategy: `plan`, `legs` (tuple of leg
  indices it serves), `activation_day`, `cost`, `comparable`, `assumptions`, `url`.
- **Coverage** — `status` (`verified`/`partial`/`unverified`), `label`, `codes`
  (ISO codes parsed from the plan name), `note`. Labeling + ambiguity only; the
  authoritative gate is destination-page membership.
- **Report** — `trip`, `generated_at`, `source_urls`, `snapshot_manifest`, `fx`,
  `recommended` (the chosen `Strategy` or None), `strategies` (the three families),
  `budget_status`, `diagnostics` (excluded_always_throttled,
  excluded_always_throttled_total, after_cap_admitted, after_cap_excluded_strict,
  data_insufficient_filtered, unpurchasable_excluded, near_matches, unservable_legs,
  regional_near_matches, regional_alternatives, fetch_failed_slugs), `totals`,
  `speed_filter_status`, `notes`. Each list is capped for display and carries a
  `*_total` alongside it.

## 4. Recommendation rules

The engine prices **every number of eSIMs** — the cheapest split of the trip's legs into
1 purchase, 2 purchases, … up to one per leg — and recommends the **cheapest feasible**
row (ranked in the requested currency). The three families the report shows are labels
over that ladder, not separate searches:

- **whole-trip** (`k = 1`) — a single plan listed on *every* leg's destination page (the
  intersection), activated once, covering all legs.
- **hybrid** (`1 < k < legs`) — any other split. It is no longer restricted to one
  multi-country plan plus singles: two regional plans covering different halves of a
  trip is a legitimate 2-eSIM answer, and pricing it is the whole point of the ladder.
- **local-only** (`k = legs`) — the cheapest data-feasible, valid plan for each leg.

A leg set needs >= 2 **legs**, not >= 2 countries: a trip that passes through the same
country twice can serve both visits with one longer plan.

The ladder is what makes the price of convenience visible. On a 32-day, four-country
trip it reads 1 eSIM £24.69 / 2 £18.63 / 3 £17.41 / 4 £17.21 / 5 £17.32 / 6 £18.90 —
so "two eSIMs instead of four" is £1.42, which no three-family table could say. Two
opt-in policies act on it, both off by default so the recommendation stays purely
cheapest:

- `--max-purchases N` — hard limit. Recommends the cheapest strategy buying at most N
  eSIMs. If nothing fits, nothing is recommended and the report names the fewest
  purchases that do work (`purchase_policy.max_purchases_satisfiable`).
- `--fewer-sims [PCT]` — buys the fewest eSIMs whose total stays within PCT% (default
  10) of the cheapest answer. Absolute fewest is *not* the goal: on the trip above the
  1-eSIM option is 43% dearer and correctly loses. The percentage is always measured
  against the *unrestricted* cheapest, even when a limit binds — otherwise `--max-purchases 1`
  would move the goalposts and call +44% "within 10%". When nothing inside a limit clears the
  tolerance the rule becomes `fewest_within_limit` and says so.

Precedence when the policies disagree: a **budget outranks convenience** (a split that busts the
budget is not bought just to save a QR code, and the budget line stays truthful), a **hard
`--max-purchases` outranks a budget** (the limit is why the flag exists; an over-budget result
under a limit names the purchase count that would have fitted), and the **coverage gate outranks
everything** (a plan not listed on a leg's page cannot serve it at any price).

Implementation (`_base_cover_costs` → `_dense_cover` → `_partition_frontier` →
`_strategy_for_split`): cheapest price per *exact* leg set as a sparse mask map, then — only for
the exact search — a superset-min over all 2^n sets (a plan proven to serve a set serves any
subset: the data need only shrinks, and activating at the earliest leg of the subset can only
leave more validity), then a DP over set partitions with an exact purchase count, taking the
lowest leg of each set as the first group so each split is visited once (3^n states). Above
`_MAX_PARTITION_LEGS` legs it restricts splits to contiguous travel windows, reading the sparse
map directly so no 2^n table is built, and says so (`totals.partition_search`). A split that buys
the same SKU twice is allowed — two copies of a plan are two legal purchases — and says so both in
the strategy's assumptions and on its frontier row (`buys_same_plan_twice`).

Feasibility rules:
- **Activation window:** a plan is installed once, `activate_lead_days` before the earliest of
  the legs it serves (**default 1** — installing needs internet, which arrival does not promise;
  0 means install on arrival), and must still be valid when the last of those legs ends:
  `activation_day + period_days - 1 >= leg.end_day`. A negative activation day is meaningful
  ("the day before the trip starts") and is rendered as such. The lead moves validity only —
  `required_gb` is a sum over the legs served, never the lead-in day — so it removes cheap
  short-validity candidates, which are the glue of many-purchase splits: on the sample trip the
  optimum goes 5 eSIMs → 4 → 3 as the lead goes 0 → 1 → 2. The clock is assumed to start at
  installation (pessimistic; a provider that starts it at first connection costs nothing to
  pre-install, and `--activate-lead-days 0` is the accurate setting for that).
- **Coverage gate (authoritative):** a plan covers a leg only if its id is listed on
  that leg's destination page. A whole-trip plan must be listed on every leg's page.
- **Speed caps (`speed_cap_signals`, default):** a plan is excluded only when it is
  throttled **from the first byte** — `speedLimit` set, or `possibleThrottling` true.
  `reducedSpeed` is the site's own "reduced speed **after the cap**" field, so a plan
  carrying only that remains a candidate: a plan that already satisfies the data
  feasibility rule below cannot drain its allowance, so its throttle is unreachable.
  Throttling text alone ("…then 384kbps", "after cap", "throttl…") still excludes, because
  no field proves *when* it starts. Any hard signal outranks `reducedSpeed`. A plan shown
  despite a cap is labelled on its own row (`⚠ then 128 kbps after the allowance`).
  `--strict-speed-cap` excludes after-allowance throttles too; `--include-capped` admits
  everything (flagged). Note "unlimited" plans have no allowance to exhaust, so a
  `speedLimit` on one is a permanent ceiling and stays excluded.
- **Purchasable:** at least one positive price in any currency.
- **Provider filter:** `--provider` restricts candidates to that provider (by name).
- **Data feasibility:** a plan serving a set of legs must cover their total data need
  (`Σ legs.required_gb`, which includes charged flying days when `transfer_data > 0`).
  A finite plan must have `effective_total_mb ≥ need`; a daily-cap plan's usable total is
  `capacity_mb × period_days`; unlimited always passes.
- **Validity:** a plan serving a set of legs, activated at the earliest leg's start, must
  not expire before the last of those legs ends — and, with charged flying days, must be
  active from the first charged pre-day and valid through the last charged tail day (a plan
  that is not yet switched on cannot pay for a flying day). Unknown validity
  (`period_days == 0`) is flagged, never blocked. A data-feasible plan that is too short is
  surfaced as a near-match, not silently dropped.
- **Dedup (cheapest per servable leg set):** for multi-leg plans, the leg set a plan is
  *listed on* is not the leg set it can *serve*. Feasibility is judged per leg set the
  plan would actually buy for, and that leg set is the dedupe key, so a plan that fails
  the full set is still tried against the windows of the trip it does cover (a 30-day
  50 GB plan on a 32-day, four-destination trip). Only maximal servable sets are kept —
  widening a set only raises its data need and lengthens the span validity must reach,
  so every servable set sits under a maximal one, and `_evaluate` re-prices the subsets
  at the same cost. A cheap plan that serves nothing is therefore pruned in favour of
  the pricier plan that lasts, as before.
  Above `_MAX_SUBSET_LEGS` legs the set search narrows to contiguous travel windows and
  the report says so (`totals.regional_scope_search`); dropped sets are counted
  (`totals.regional_candidates_truncated`), never silently cut.
- **What was ruled out, and why:** two diagnostics buckets answer "why is plan X not an
  option" for multi-leg plans. `regional_near_matches` holds plans listed on >= 2
  *different countries* with enough data for everything they are listed on but too short
  for the calendar, sorted by how many days they fall short (`legs_run_to_day`,
  `short_by_days`). `regional_alternatives` holds plans that **are** feasible for some
  leg set but were not bought by any family's winning strategy — usually a pricier
  whole-trip option, or one covering a different window. Same-country visit pairs are
  excluded from the first bucket (a plan too short for both visits is still fine for one,
  and per-leg picks already price it).

**Budget gate:** with `--budget`, the recommended strategy is compared against the budget;
`budget_status` reports `within_original` and, if over, the compound 10% steps to relax
the cap. No purchase is attempted — this only labels the result.

## 5. Provenance & reproducibility

Every HTTP response body (countries, regions, each destination's plans, and the FX rate)
is written to `.cache/` (raw bytes) plus a sidecar manifest containing `url`,
`fetched_at`, `sha256`, and `bytes`. `fetch_json` returns the manifest so the report can
cite exactly what it used. Each `Report` carries:

- `source_urls` — every endpoint hit (countries, regions, each destination's plans, and
  the FX endpoint).
- `snapshot_manifest` — per URL: the local snapshot path, SHA-256, fetch time, byte size,
  and `source` (`fetched` vs `cache`).
- `fx` — the FX source, rate, currency, date, and staleness used for conversion.
- `generated_at` and `notes` (disclaimers: read-only, prices may change, coverage is
  verified by site listing).

Because the cache is keyed by URL and reports record hashes, a run is reproducible offline
(reuse `.cache`), and `--refresh` refreshes the snapshots. The FX rate is cached for 12
hours. The SHA-256 in a report lets you verify the cached bytes match what was used.

## 6. Limitations

- **Not a stable public API.** The folded endpoints are internal client endpoints; field
  names or layout could change, which would break parsing. This is accepted for a
  read-only research tool.
- **Prices are point-in-time.** Captured at fetch time and may change; confirm before
  purchase.
- **Name-based scope is a heuristic.** "33 countries", "Europe Lite", exclusions, etc. are
  best-effort; the site listing is authoritative, and ambiguous names are flagged rather
  than guessed.
- **Currency display.** The requested currency is shown when present, else USD, else the
  plan's first currency; a small FX fallback is used only for *ranking* non-USD-priced
  plans.
- **No purchase/activation.** Read-only by design.
