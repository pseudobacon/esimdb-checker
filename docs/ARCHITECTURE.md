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
  config.py     base URL, endpoints, timeouts, retries, default data dir
  http.py       httpx client builder + fetch-with-cache (raw bytes -> snapshot)
  snapshots.py  cache paths, read/write cache, SHA-256 manifest
  geo.py        Country/region loading + name/code/slug resolution + region membership
  catalog.py    parse the folded payload into Plan objects
  models.py     dataclasses: Country, Plan, Coverage, Itinerary, Recommendation, Report
  coverage.py   plan-name scope resolution (label + ambiguity flags)
  filters.py    speed-cap detection, purchasability, data-need check
  recommend.py  RecommendationEngine: fetch -> intersect -> filter -> rank -> Report
  report.py     console / markdown / JSON renderers
  cli.py        argparse CLI (recommend, plans, countries, resolve, fetch)
```

Data flow for `recommend`:

```
CLI (args) -> Itinerary
  -> Geo.load (countries + regions, cached)
  -> for each destination slug: fetch_country_plans (folded, cached) -> Plan[]
  -> pool = intersection of per-destination plan-id sets   (coverage gate)
  -> drop speed-capped (record reasons)
  -> drop unpurchasable (no positive price)
  -> for each candidate: coverage label, meets-data-need, validity, caveats
  -> tiered, provider-diverse selection (top N)
  -> Report (winners, excluded_speed_cap, unverified_coverage, fallback, totals,
             provenance)
  -> render (console / json / markdown)
```

## 3. Data model (`models.py`)

- **Country** — `code` (ISO, e.g. `ES`), `name`, `slug`, `region`, `subregions`,
  `popularity`.
- **Plan** — `id`, `name`, `provider`, `provider_slug`, `capacity_mb` (0 = unlimited),
  `period_days` (0 = unknown), `provider_popularity`, `prices` (per-currency dict),
  `data_cap_per`, `speed_limit`/`reduced_speed` (kbps), `possible_throttling`, `tethering`,
  `has_5g`, `is_low_latency`, `subscription`/`subscription_period`, `networks`, `has_ads`,
  `pay_as_you_go`, `new_user_only`, `notes`, `source_slug`.
  Derived: `usd_price`, `usd_ranking_price()` (USD or FX fallback), `price_and_currency()`,
  `unlimited`, `capacity_label()`, `period_label()`, `data_cap_label()`,
  `price_per_gb_usd()`, `effective_total_mb(days)`.
- **Coverage** — `status` (`verified`/`partial`/`unverified`), `label`, `codes`
  (ISO codes parsed from the plan name), `note`. Labeling + ambiguity only; the
  authoritative gate is destination-page membership.
- **Itinerary** — `countries`, `days`, `data_gb`, `currency`.
- **Recommendation** — `rank`, `plan`, `coverage`, `usd_price`, `display_price`,
  `currency`, `price_per_gb`, `meets_data_need`, `reasons`, `caveats`, `url`.
- **Report** — `itinerary`, `generated_at`, `source_urls`, `snapshot_manifest`, `totals`,
  `winners`, `excluded_speed_cap`, `unverified_coverage`, `fallback`, `notes`.

## 4. Recommendation rules

- **Coverage gate (authoritative):** a plan is eligible only if its id is on *every*
  itinerary destination's page (intersection). This mirrors esimdb's own coverage.
- **Speed caps (strict, default):** excluded when any of `speedLimit` is set,
  `reducedSpeed` is set, `possibleThrottling` is true, or strong throttling text
  ("…then 384kbps", "after cap", "throttl…") appears in the name/notes. Each exclusion is
  recorded with the specific reason(s). `--include-capped` includes them (flagged).
- **Purchasable:** at least one positive price in any currency; new-user-only /
  pay-as-you-go / ad-bearing plans add caveats.
- **Meets data need:** unlimited always passes; a daily-cap plan passes if
  `capacity_mb × min(validity, trip_days) ≥ need`; a finite plan passes if its capacity ≥
  need.
- **Validity:** `period_days == 0` (unknown) or `≥ trip_days` is "spans the trip"; a shorter
  fixed validity is a caveat (may expire mid-trip).
- **Ranking:** candidates are tiered (meets need + spans trip → meets need → below need),
  then ordered by (uncapped first, USD price, USD/GB, provider popularity, longer validity,
  larger capacity). Selection is **provider-diverse**: the best plan per distinct provider
  leads, then the next-best fill remaining slots (so a single provider can't fill the top).

## 5. Provenance & reproducibility

Every HTTP response body is written to `.cache/` (raw bytes) plus a sidecar manifest
containing `url`, `fetched_at`, `sha256`, and `bytes`. `fetch_json` returns the manifest so
the report can cite exactly what it used. Each `Report` carries:

- `source_urls` — every endpoint hit (countries, regions, and each destination's plans).
- `snapshot_manifest` — per URL: the local snapshot path, SHA-256, fetch time, byte size,
  and `source` (`fetched` vs `cache`).
- `generated_at` and `notes` (disclaimers: read-only, prices may change, coverage is
  verified by site listing).

Because the cache is keyed by URL and reports record hashes, a run is reproducible offline
(reuse `.cache`), and `--no-cache` refreshes the snapshots. The SHA-256 in a report lets you
verify the cached bytes match what was used.

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
