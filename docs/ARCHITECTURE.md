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
  filters.py    speed-cap detection, purchasability, data-need check
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

The TUI never renders the report: the search runs after the alternate screen is restored,
so the report is printed once on the normal screen and remains visible in scrollback. An
exception inside the TUI cannot leave the terminal grabbed — Textual unwinds its alternate
screen context, `run_tui_wizard` swallows `KeyboardInterrupt`/`EOFError` into a cancelled
result, and `cmd_wizard` catches anything else and falls back to the plain interview.

Two rendering rules keep the TUI usable; both were found by driving it in a real
pseudo-terminal and are invisible to a widget-level test:

- Any string containing literal `[...]` (button names in status/help/body lines) goes to
  `Static.update()` as a `rich.text.Text`, never a bare `str` — as a `str`, Rich parses the
  brackets as markup tags and the hint silently vanishes from the screen.
- `Horizontal` children need an explicit CSS width (`#inputs Input { width: 1fr; }`);
  without it every `Input` claims the whole row and only the first field is visible.

Data flow for `recommend`:

```
CLI (args) -> parse_stops / build_trip -> Trip (legs with per-destination durations)
  -> Geo.load (countries + regions, cached)
  -> fx.fetch_fx (Frankfurter rate, cached)
  -> for each leg's slug: fetch_country_plans (folded, cached) -> Plan[]
  -> plan coverage = the set of leg pages each plan is listed on
  -> drop speed-capped + unpurchasable (+ optional provider filter)
  -> local picks: cheapest data-feasible, valid plan per leg
  -> regional candidates: cheapest data-feasible, valid plan per coverage scope
  -> build 3 families: local-only, whole-trip, hybrid
  -> recommend = cheapest feasible family (in --currency); gate on --budget
  -> Report (strategies, recommended, budget_status, diagnostics, totals, provenance)
  -> render (console / json / markdown)
```

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
  transfer leg), `transfer_days` (days before this leg, not charged), `start_day`,
  `end_day`, `required_gb` (`days × daily_data_gb`), `country_name`, `is_transfer`.
- **Trip** — the per-destination itinerary: `legs` (ordered), `total_days` (charged days),
  `required_gb`, `daily_data_gb`, `currency`, `budget` (None = no budget), `start_date`,
  `end_date`; `.distinct_countries` is the ordered unique destination set.
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
  `budget_status`, `diagnostics` (excluded_speed_cap, unpurchasable_excluded,
  near_matches, unservable_legs, fetch_failed_slugs), `totals`, `speed_filter_status`,
  `notes`.

## 4. Recommendation rules

The engine builds three independent strategy families from the acquired plans and
recommends the **cheapest feasible** one (ranked in the requested currency):

- **local-only** — the cheapest data-feasible, valid plan for each leg.
- **whole-trip** — a single plan listed on *every* leg's destination page (the
  intersection), activated once, covering all legs.
- **hybrid** — one regional plan serving a subset of legs (>= 2 destinations) plus
  per-destination local plans for the rest.

Feasibility rules:
- **Coverage gate (authoritative):** a plan covers a leg only if its id is listed on
  that leg's destination page. A whole-trip plan must be listed on every leg's page.
- **Speed caps (strict, default):** excluded when any of `speedLimit` is set,
  `reducedSpeed` is set, `possibleThrottling` is true, or strong throttling text
  ("…then 384kbps", "after cap", "throttl…") appears in the name/notes. `--include-capped`
  includes them (flagged).
- **Purchasable:** at least one positive price in any currency.
- **Provider filter:** `--provider` restricts candidates to that provider (by name).
- **Data feasibility:** a plan serving a set of legs must cover their total data need
  (`Σ legs.days × daily_data_gb`). A finite plan must have `effective_total_mb ≥ need`;
  a daily-cap plan's usable total is `capacity_mb × period_days`; unlimited always
  passes.
- **Validity:** a plan serving a set of legs, activated at the earliest leg's start, must
  not expire before the last of those legs ends. Unknown validity (`period_days == 0`)
  is flagged, never blocked. A data-feasible plan that is too short is surfaced as a
  near-match, not silently dropped.
- **Dedup (cheapest valid per scope):** for regional plans, only the cheapest
  data-feasible, valid plan per coverage scope is kept — so a cheap plan that expires
  mid-trip is pruned in favour of the pricier plan that actually lasts.

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
