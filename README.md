# esimdb-checker

A local CLI that researches travel eSIM plans on **https://esimdb.com/** and recommends a
purchase strategy for a multi-destination itinerary. It is **research/recommend only** —
it never buys, activates, or modifies anything on the site.

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
destination, so a plan is verified to cover a leg if its id appears on *that leg's*
destination page. For a multi-destination trip, a plan covers the whole trip only if it is
listed on *every* destination's page — the **intersection** of the per-destination plan
sets — which is esimdb.com's own coverage assertion. Plan-name scope (e.g. "Europe",
"Global") is parsed separately only to label the scope and to flag ambiguous names
("partial" / "unverified"); it is never the authoritative gate.

**Speed caps (strict filter).** Plans with a speed cap are excluded by default and
reported with reasons. Caps are detected from three independent signals so that a plan
hidden by the site's own "Hide plans with speed caps" toggle is still caught:
`speedLimit`, `reducedSpeed`, `possibleThrottling`, plus strong throttling text in the
plan name/notes. `--include-capped` relaxes this (capped plans are then shown, flagged).

**Pricing & currency.** Prices are stored per currency (USD, EUR, GBP, JPY, …) exactly as
the site provides them. Every plan is converted to the requested `--currency` using the
**Frankfurter** API (European Central Bank reference rates, cached for 12 hours), with a
small built-in fallback table if FX is unavailable. Ranking, budget, and totals are all
compared in one currency; display shows the requested currency (USD in parentheses when it
differs).

More detail, exact filter rules, and limitations: [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

## Install

```bash
uv sync                 # creates .venv; installs httpx + textual (dev: pytest, respx, ruff)
uv pip install -e .     # or: install the `esimdb-checker` console script
```

Then run with `.venv/bin/esimdb-checker …`, or `uv run esimdb-checker …`.

## Usage

```bash
# Single country, 9 days, ~1.5 GB/day, in EUR
esimdb-checker recommend spain --days 9 --daily-data 1.5 --currency EUR

# Multi-destination trip: 4 days Spain, then an uncharged transfer marker, then
# 7 days France and 3 days Germany, with a total budget of 200 (in EUR)
esimdb-checker recommend --stop 'Spain 4' --stop '' --stop 'France 7' --stop 'Germany 3' \
    --daily-data 1.5 --currency EUR --budget 200

# Ask for GBP everywhere (validated: only real ISO codes are accepted)
esimdb-checker recommend spain --days 9 --currency GBP --budget 150

# Clean JSON on stdout, Markdown report in a file
esimdb-checker recommend spain --days 9 --daily-data 1.5 --json > report.json
esimdb-checker recommend spain --days 9 --daily-data 1.5 --md --out report.md

# Restrict to one provider and include speed-capped plans (flagged)
esimdb-checker recommend spain --days 9 --provider "Airalo" --include-capped

# Interactive itinerary wizard (in-place terminal UI, then the same report)
esimdb-checker recommend-wizard

# ... in a different currency, saving the itinerary for reuse later
esimdb-checker recommend-wizard --currency USD --save-file itinerary.json

# Reuse (and edit) a saved itinerary non-interactively
esimdb-checker recommend --itinerary itinerary.json
```

Destinations accept country **names**, **ISO codes**, or **slugs** (case-insensitive).
For the multi-leg form, each `--stop` is `'Country N'` (name/code/slug plus a day count);
a **blank** `--stop ''` is a transfer/overnight marker leg that is not charged, and
`'Country N +T'` adds *T* uncharged transfer days before that leg. The single-position
form (`recommend spain --days 9`) is a shorthand for one `--stop`.

**Currencies.** Prices, ranking, and the budget all use one validated currency. The
supported set is the Frankfurter/ECB list the tool can actually convert to:

> `AUD BRL CAD CHF CNY CZK DKK EUR GBP HKD HUF IDR ILS INR ISK JPY KRW MXN MYR NOK NZD PHP PLN RON SEK SGD THB TRY USD ZAR`

Codes are case-insensitive (`--currency gbp` works). An unsupported or numeric value
(`--currency 15`) is rejected at parse time with the supported list, and a saved itinerary
containing a bad currency is reported instead of guessed.

Browse and inspect:

```bash
esimdb-checker countries                  # list destinations (code/name/slug/region)
esimdb-checker countries --region europe --query "united"
esimdb-checker resolve "United Kingdom"   # resolve one name/code/slug token
esimdb-checker resolve IT
esimdb-checker plans spain --limit 25 --currency EUR
esimdb-checker fetch "https://esimdb.com/api/client/countries"
```

### `recommend` options

| Option | Meaning | Default |
| --- | --- | --- |
| `destinations` (positional, 0–1) | single destination (name/code/slug), used with `--days` | — |
| `--stop 'Country N'` | one itinerary leg; repeat for multiple (`''` = transfer leg, `'N +T'` = T uncharged transfer days) | — |
| `--itinerary FILE` | load a saved itinerary (from the wizard) instead of `--stop` | — |
| `--days` | length of the single positional destination | — |
| `--daily-data` | expected data per destination day, in GB | `1.5` |
| `--currency` | currency for prices, ranking, budget, and totals | `EUR` |
| `--budget` | optional total-trip budget | none |
| `--budget-currency` | currency `--budget` is expressed in (converted for comparison) | `--currency` |
| `--provider` | restrict to one provider (name or slug) | all |
| `--include-capped` | include speed-capped plans (shown, flagged) | off |
| `--json` | print the full typed report as JSON to stdout | console text |
| `--md` | write a Markdown report (with `--out`, default `report.md`) | — |
| `--out FILE` | output file for `--md`, or for console text when `--md`/`--json` are absent; with `--json` also copies the JSON there while stdout stays clean | — |
| `--save-itinerary FILE` | save the built itinerary to FILE (JSON) | — |
| `--overwrite` | allow saving to overwrite an existing file | off |
| `--refresh` | refetch even if a snapshot exists | cache on |
| `--data-dir` | where snapshots are stored (global flag, before the subcommand) | `.cache` |

`--json` and `--md` are mutually exclusive; an empty `--out` is rejected. Both exit 2
before any network work. The same global `--data-dir` / `--refresh` apply to `plans`,
`countries`, `resolve`, and `fetch`.

### `recommend-wizard` (interactive)

An in-place terminal UI (built with [Textual](https://textual.textualize.io/)) for the same
itinerary model, then the same report:

```bash
esimdb-checker recommend-wizard                          # TUI (needs a terminal)
esimdb-checker recommend-wizard --currency USD           # offer USD first
esimdb-checker recommend-wizard --currency EUR --budget 200 --daily-data 2
esimdb-checker recommend-wizard --itinerary trip.json    # resume/edit a saved itinerary
esimdb-checker recommend-wizard --save-file trip.json    # save the confirmed itinerary
esimdb-checker recommend-wizard --plain                  # line-oriented fallback
esimdb-checker recommend-wizard --json > report.json     # interview on stderr, JSON on stdout
esimdb-checker recommend-wizard --md --out report.md     # write a Markdown report
```

Three named stages — **Destinations → Preferences → Review** — shown as a progress line:

1. **Destinations** — type a destination plus its days and transfer days, then *Add leg*.
   Transfer days are asked **per leg** and are **never charged** (they still shift the
   travel-day spans shown in the table). *+ transfer* inserts a standalone uncharged
   marker leg; *edit* / *remove* change or drop a leg (type its number, then press the same
   key again or Enter). Nothing is searched until you continue.
2. **Preferences** — currency (defaults to `EUR`, validated against the supported list; a
   bad code keeps you on the same line with an explanation), total budget **in that same
   currency** (blank = no cap), then an expected-data level chosen with the arrow keys:
   *Economy 1 GB/day*, *Balanced 1.5 GB/day*, *Higher usage 2 GB/day* — with the resulting
   trip total shown (e.g. a 34-day trip at 2 GB/day is 68 GB).
3. **Review** — the derived itinerary (legs, charged days, travel-day spans, required GB,
   currency, budget). *Save itinerary* writes the reusable JSON file; *search this
   itinerary* closes the UI and prints the report **once** on the normal screen, so it
   stays visible and scrollback works. At the save prompt a path is used as given, a bare
   `y` (or Enter in the TUI) saves as `itinerary.json`, and `n` skips — yes/no answers are
   never turned into filenames.

Keyboard: `Tab`/`Shift+Tab` move between controls, `Enter` confirms the current stage or
the focused button, `↑`/`↓` choose the data level, `Esc` cancels. Invalid input is reported
in the current stage and nothing is lost; going *< back* returns with every entered value
kept.

The wizard always produces the same `Trip` the non-interactive path builds — the validation,
itinerary maths, saving, and recommendation are shared, never duplicated.

**Fallbacks and scripting.** The TUI is used only when stdin, stdout, and stderr are all
screens and `--json` is not requested. `--plain` (or piped stdout / redirected stderr /
`--json`) uses the line-oriented interview instead, with its questions on **stderr** so
stdout can carry clean JSON. If stdout is not a screen, the plain interview starts
automatically. If the TUI cannot start at all, the wizard says so and falls back to plain
mode. With no interactive stdin **and no** `--plain`, it fails immediately (exit 2) and
prints the non-interactive equivalents (`recommend --stop …` / `recommend --itinerary …`);
with `--plain` explicitly requested, a pipe or file of answers is read instead, ending
cleanly at EOF.

| Option | Meaning |
| --- | --- |
| `--itinerary FILE` | start from a saved itinerary and edit it |
| `--currency` | currency offered first (validated; default `EUR`) |
| `--budget`, `--daily-data` | prefill those answers |
| `--plain` | force the line-oriented interview |
| `--save-file FILE`, `--overwrite` | save the confirmed itinerary |
| `--provider`, `--include-capped`, `--refresh`, `--data-dir` | same meaning as for `recommend` (`--data-dir` is global: put it **before** the subcommand) |
| `--json`, `--md`, `--out` | shared output options (see above) |

### Reading a recommendation

`recommend` builds three independent strategy families and recommends the cheapest feasible
one (ranked in the requested currency):

- **local-only** — one plan per destination.
- **whole-trip** — a single plan listed on every destination's page (activated once).
- **hybrid** — one regional plan for a subset of destinations plus per-destination plans
  for the rest.

Each plan shows its activation day, which legs it serves (and their travel days), and any
assumptions (e.g. a validity that must span the legs). A plan must be **data-feasible** and
**valid** (not expire before the legs it serves end) to be used for those legs.
Speed-capped plans and unpurchasable plans are never silent — they appear in
`excluded_speed_cap` / `unpurchasable_excluded`, and a data-feasible-but-too-short plan is
surfaced under `near_matches`. With `--budget`, the `budget_status` section reports whether
the cheapest strategy is within budget (and, if not, the compound 10% steps needed to relax
it).

## Testing

```bash
uv run pytest                 # 132 offline tests (mocked API + headless TUI), no network
uv run ruff check src tests   # lint
```

The suite mocks the observed API with `respx` and synthetic folded payloads, covering
parsing, country/region resolution, the itinerary/stop model, speed-cap detection, the
three strategy families (dedup, validity, budget, provider filter), and the CLI. The
wizard is covered twice: `tests/test_wizard.py` drives the plain front-end with scripted
input, `tests/test_wizard_tui.py` drives the real Textual widgets through `App.run_test`
(Textual's headless driver), and `tests/test_cli_wizard.py` drives the CLI wizard path —
including the output modes — through a fake TTY. The TUI tests pause briefly around clicks
and keyboard confirms because Textual debounces a second click on a button that is still
playing its press animation; that is why the suite takes ~50 s instead of a few seconds.

### Verified in a terminal

Headless widget tests cannot see what a terminal actually shows, so the wizard was also
driven inside a **real pseudo-terminal** (`pty.fork()` + a 110×34 window, with the output
reconstructed through the `pyte` terminal emulator). Keyboard-only, no mouse: destination +
days + transfer days → `Tab` → `Enter`, `Tab` to *next →*, currency, budget, `↑`/`↓` for the
data level, review, search. That check confirmed:

- the alternate screen is entered **and left** (`?1049h`/`?1049l`) and mouse tracking is set
  **and cleared**, so the shell is never left grabbed;
- all three input boxes render side by side, and the button-name hints (`[add leg]`,
  `[use currency ->]`, `[search this itinerary]`) appear literally;
- after the search, the full report is printed on the normal screen with the wizard's
currency, per-destination durations, and provenance block.

It also found two real defects that no headless test could see — Rich was swallowing the
`[button]` hints as markup, and the input row had no width rule — both fixed and covered by
regression tests in `tests/test_wizard_tui.py`.

What was **not** verified: this environment has no human-observable display, so colours,
spacing, resize behaviour, and mouse interaction were not eyeballed; and the pty harness
could not read the child's exit status back (the report text and the restored screen were
checked instead). Everything else here is machine-checked.

### Verified commands

These were run against the live site (results depend on current prices):

```bash
# 1. Interactive wizard: change the currency, arrow-key to 2 GB/day, save, get a report
esimdb-checker recommend-wizard --currency USD --save-file trip.json

# 2. Same itinerary, resumed and edited in the wizard
esimdb-checker recommend-wizard --itinerary trip.json

# 3. Plain fallback (same questions, no terminal UI) and clean JSON
esimdb-checker recommend-wizard --plain < answers.txt   # explicit --plain reads a pipe
esimdb-checker recommend-wizard --json > report.json     # questions go to stderr

# 4. Reports in every format
esimdb-checker recommend-wizard --md --out report.md
esimdb-checker recommend --itinerary trip.json --json | jq '.recommended.total'

# 5. Non-interactive equivalents (no terminal required)
esimdb-checker recommend --stop 'Spain 4' --stop 'France 7' --currency GBP --daily-data 2
esimdb-checker recommend --itinerary trip.json --currency EUR --daily-data 1

# 6. Invalid input is reported, never guessed
esimdb-checker recommend spain --days 4 --currency 15      # exits 2, lists supported codes
esimdb-checker recommend --itinerary itinerary.json        # "currency": "15" -> explained
```

Live verification of the recommendation data itself:

```bash
uv run esimdb-checker recommend spain --days 9 --daily-data 1.5 --currency EUR
```

## Reproducibility

Every HTTP response body (countries, regions, each destination's plans, and the FX rate) is
snapshotted to `.cache/` (raw bytes + a manifest with URL, fetch time, SHA-256, and
byte size). Each report records the exact snapshot paths, hashes, and fetch timestamps it
used, so a recommendation can be reproduced offline from the captured data (rerun with
`--refresh` to refresh). The FX rate is cached for 12 hours and is also cited in the
report.
