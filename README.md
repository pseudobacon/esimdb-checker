# esimdb-checker

A local CLI that researches travel eSIM plans on **https://esimdb.com/** and recommends a
whole-itinerary purchase strategy. It is **research/recommend only** — it never buys,
activates, or modifies anything on the site.

> **Safety / scope.** This tool reads public data and prints recommendations. It performs
> read-only HTTP GET requests against the structured JSON endpoints the esimdb.com website
> itself uses. It does not log in, and it does not place orders.

## How it gets data (important)

`esimdb.com` is a Nuxt app whose DOM is virtualized (only ~600 rows ever render, in an
order that is **not** a complete catalog). Following the task guidance, this tool does
**not** scrape the DOM. Instead it calls the **observed client JSON API** that the site
fetches during a normal page load:

- `GET /api/client/countries`        — all destinations (code, name, slug, region)
- `GET /api/client/regions`          — regions + their full country lists
- `GET /api/client/countries/<slug>/data-plans/folded?locale=en` — the structured plan
  catalog for a destination (capacity, period, multi-currency prices, speed limit,
  reduced speed, throttling, daily cap, tethering, provider, networks).

The folded plan payload is compact/positional (a `families` array + `rows` that reference a
family by index, plus a `providers` map). `src/esimdb_checker/catalog.py` parses it into
`Plan` objects.

**Coverage model.** A destination page only lists plans that are usable in that
destination, so a plan is verified to cover an itinerary if its id appears on *every*
itinerary destination's page — i.e. in the **intersection** of the per-destination plan
sets. This is esimdb.com's own coverage assertion. Plan-name scope (e.g. "Europe",
"Global") is parsed separately only to label the scope and to flag ambiguous names
("partial" / "unverified"); it is never the authoritative gate.

**Speed caps (strict filter).** Plans with a speed cap are excluded by default and
reported with reasons. Caps are detected from three independent signals so that a plan
hidden by the site's own "Hide plans with speed caps" toggle is still caught:
`speedLimit`, `reducedSpeed`, `possibleThrottling`, plus strong throttling text in the
plan name/notes. `--include-capped` relaxes this (capped plans are then shown, flagged).

**Pricing.** Prices are stored per currency (USD, EUR, GBP, JPY, …) exactly as the site
provides them. The requested `--currency` is shown first, falling back to USD, then the
plan's first available currency. Ranking always compares a USD value, using a small
fallback FX table only for plans that have no USD price (display is never converted).

More detail, exact filter rules, and limitations: [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

## Install

```bash
uv sync            # creates .venv; installs httpx (dev deps: pytest, respx)
uv pip install -e .   # or: install the `esimdb-checker` console script
```

Then run with `.venv/bin/esimdb-checker …`, or `uv run esimdb-checker …`.

## Usage

```bash
# Single country, 9 days, ~15 GB, in EUR
esimdb-checker recommend spain --days 9 --data-gb 15 --currency EUR

# Multi-country trip (Spain, France, Germany), 10 days, ~40 GB
esimdb-checker recommend Spain France Germany --days 10 --data-gb 40 --currency EUR

# Fewer picks, JSON to stdout, markdown report to a file
esimdb-checker recommend Spain France --top 3 --json > report.json
esimdb-checker recommend Spain France --top 3 --md report.md

# Restrict to one provider and include speed-capped plans (flagged)
esimdb-checker recommend Spain --provider "Airalo" --include-capped
```

Destinations accept country **names**, **ISO codes**, or **slugs** (case-insensitive), one
per token: `Spain`, `ES`, `spain` all work.

Browse and inspect:

```bash
esimdb-checker countries                  # list destinations (code/name/slug/region)
esimdb-checker countries --region europe --query "united"
esimdb-checker resolve "United Kingdom" UK   # resolve name/code/slug tokens
esimdb-checker plans spain --limit 25 --currency EUR
esimdb-checker fetch "https://esimdb.com/api/client/countries"
```

### `recommend` options

| Option | Meaning | Default |
| --- | --- | --- |
| `countries` (positional, one or more) | itinerary destinations (names, codes, or slugs) | — |
| `--days` | trip length in days | `7` |
| `--data-gb` | total data to cover, in GB | `10` |
| `--currency` | display currency (`EUR`/`USD`/…) | `EUR` |
| `--top` | number of recommendations | `5` |
| `--provider` | restrict to one provider (by name) | all |
| `--include-capped` | include speed-capped plans (shown, flagged) | off |
| `--json` | print the report as JSON | console |
| `--md FILE` | also write a markdown report to FILE | — |
| `--no-cache` | refetch even if a snapshot exists | cache on |
| `--data-dir` | where snapshots are stored (global flag, before the subcommand) | `.cache` |

The same global `--data-dir` / `--no-cache` apply to `plans`, `countries`, `resolve`, and
`fetch`.

### Reading a recommendation

Each pick shows: rank, provider, plan name, price (in the requested currency, with USD in
parentheses when different), data capacity, validity, price-per-GB, the coverage label,
and reasons/caveats. Recommendations are **provider-diverse** (the best plan per provider
leads, then the next-best fill remaining slots) and are tiered by (meets the data need,
validity spans the trip, price, value). If no single plan covers the whole itinerary, the
report lists **per-destination fallbacks** instead. Speed-capped plans and plans whose
name scope is ambiguous are never silent — they appear in `excluded_speed_cap` /
`unverified_coverage` (JSON) or in the console sections.

## Testing

```bash
uv run pytest            # offline unit tests (mocked API, no network)
```

The suite mocks the observed API with `respx` and synthetic folded payloads, covering
parsing, coverage resolution, speed-cap detection, the intersection + ranking engine,
and the CLI. Live verification is done by running the CLI against the real site:

```bash
uv run esimdb-checker recommend Spain France --days 9 --data-gb 15 --currency EUR
```

## Reproducibility

Every fetch is snapshotted to `.cache/` (raw bytes + a manifest with URL, fetch time, and
SHA-256). Each report records the exact snapshot paths, hashes, and fetch timestamps it
used, so a recommendation can be reproduced offline from the captured data (rerun with
`--no-cache` to refresh).
