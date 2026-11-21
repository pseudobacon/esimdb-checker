# HANDOFF — current state

Compact snapshot. Design detail lives in [ARCHITECTURE.md](ARCHITECTURE.md); the dated
history lives in [DEVELOPMENT-LOG.md](DEVELOPMENT-LOG.md).

Snapshot date: **2026-11-21**.

## What this is

A local, keyboard-driven CLI that reads esimdb.com's own JSON endpoints and recommends a
whole-itinerary eSIM purchase strategy (local / whole-trip / hybrid) against destinations,
day counts, data need, validity and budget. Research only — it never buys or activates.

## Current state

| | |
| --- | --- |
| Package | `esimdb-checker` 0.1.0, `src/` layout, Python 3.10+, console script `esimdb-checker` |
| Runtime deps | `httpx`, `textual` — **no browser, no Playwright at runtime** |
| Tests | **195 passing**, fully offline (respx-mocked API, Textual headless Pilot) |
| Lint | `ruff check .` **clean, exit 0**. Pre-existing debt (174 findings at the baseline commit) swept in two passes — see DEVELOPMENT-LOG. |
| Type check | none configured (`mypy` not installed, no config) |
| CI | none |
| Publishing | Published at `https://github.com/pseudobacon/esimdb-checker` — **public** (chosen deliberately), default branch `main`, `origin` points there |

## Checks that were actually run (2026-11-22, re-run after review)

```
python -m pytest tests/ -p no:cacheprovider   -> 192 passed in 82.57s
python -m ruff check .                        -> All checks passed (exit 0)
esimdb-checker recommend --itinerary asia-2026.itinerary.json
     -> Cost of convenience: 1 £24.69 / 2 £18.63 / 3 £17.19 / 4 £17.21 / 5 £17.12 / 6 £18.70
esimdb-checker recommend … --fewer-sims        -> 2 eSIMs £18.63 (10% tolerance)
esimdb-checker recommend … --max-purchases 2   -> 2 eSIMs; --max-purchases 1 -> nothing recommended
esimdb-checker recommend … --fewer-sims 3 --max-purchases 4 -> 3 eSIMs, both notes shown
esimdb-checker recommend … --fewer-sims 10 --max-purchases 1 -> rule fewest_within_limit,
     tolerance_met false, header "fewest eSIMs the 1-eSIM limit allows" (no false 10% claim)
esimdb-checker recommend … --budget 18         -> budget £18.00 respected (this path used to crash)
esimdb-checker recommend … --budget 17.5 --max-purchases 2
     -> "A 3-purchase strategy at £17.19 would fit - a purchase policy is what ruled it out"
esimdb-checker recommend … --budget 18 --fewer-sims -> 5 purchases: the 2-eSIM split busts £18
esimdb-checker recommend … --activate-lead-days 1 -> 4 purchases £17.21 (lead 0: 5 £17.12;
     lead 2: 3 £17.41) — a longer install lead buys FEWER eSIMs; see DEVELOPMENT-LOG 2026-11-22
python -m ruff check .                        -> All checks passed (exit 0)
esimdb-checker recommend --itinerary …        -> console, --json, --md, --strict-speed-cap, --include-capped
pty-driven wizard walkthrough                 -> see README "Verified in a terminal"
```

## Where things live

- `src/esimdb_checker/cli.py` — argparse surface; `recommend`, `recommend-wizard`, `plans`,
  `countries`, `resolve`, `fetch`. Shared engine flags in `_add_engine_args`.
- `recommend.py` — acquisition, candidate generation, the purchase-count frontier
  (`_cover_costs` → `_partition_frontier`), policies (`_choose`), diagnostics, totals.
- `filters.py` — `speed_cap_signals` (always-throttled vs after-allowance), `purchasable`,
  `meets_data_need_for_days`.
- `wizard.py` — Textual TUI + `--plain` fallback; both build the same `Trip`.
- `report.py` — console / JSON / Markdown. `_diagnostics_lines()` is shared by console and
  Markdown so the two renderers cannot disagree.
- `itinerary.py`, `models.py` — itinerary model and validation (shared by wizard and CLI).

## Decisions worth inheriting

1. **Coverage is a gate, labels are not.** A plan serves a leg only if its id appears on that
   destination's page. Name/ISO matching only *labels* and warns.
2. **Data need is a pre-filter, not a diagnostic row.** Plans below the trip's need are counted
   (`filtered before ranking…`), never enumerated. The enumerated bucket is exactly "met this
   trip's data need, removed by the cap".
3. **Speed caps are two-tier by default.** Throttled *from the first byte* (`speedLimit`,
   `possibleThrottling`) → excluded. Throttled *after the allowance* (`reducedSpeed`) → kept and
   flagged `⚠ then 128 kbps after the allowance`, because a plan that already covers the need
   cannot reach that throttle. `--strict-speed-cap` restores the old rule; `--include-capped`
   admits everything. Unlimited plans have no allowance to exhaust, so a `speedLimit` on one is
   permanent and stays excluded.
4. **Every count is a total, never a list length.** Detail rows are capped by `_DETAIL_CAP`;
   `*_total` keys carry the real figures. Two truncation bugs of this class were found and fixed.
5. **The wizard is keyboard-only.** No buttons, no mouse capture (verified: no mouse-tracking
   sequence in the byte stream), so native terminal copy still works.
7. **A plan's page set is not its servable set.** Multi-leg candidates are generated per
   leg set the plan would actually be bought for, not per the legs it happens to be listed
   on: feasibility is re-checked per set, only maximal servable sets are kept as dedupe
   keys, and >= 2 **legs** (not countries) qualifies — so repeat visits share one plan and a
   plan too short for the whole trip can still serve a window of it. Rejected multi-leg
   plans are explained by `regional_near_matches` / `regional_alternatives` rather than
   vanishing. See DEVELOPMENT-LOG 2026-11-21.
8. **The families are labels over one search, not three searches.** The engine prices every
   purchase count (1 eSIM … one per leg) and calls k=1 `whole_trip`, k=legs `local_only`, the
   rest `hybrid`. They used to be enumerated separately, which meant a 2-eSIM answer was
   unpriceable and often unfindable — the search bought at most one multi-country plan. Cheapest
   stays the default recommendation; `--max-purchases` (hard limit) and `--fewer-sims [PCT]`
   (fewest within a tolerance, default 10%) are opt-in, and a limit nothing fits recommends
   nothing rather than quietly exceeding it. A split may buy the same SKU twice; that is legal
   and says so in the assumptions. See DEVELOPMENT-LOG 2026-11-22.
9. **Provenance is part of the output.** Snapshots, SHA-256, fetch times and the FX rate are in
   every report; `speed_filter_status.mode` always states `local_predicate` vs
   `verified_site_toggle` honestly.
10. **The convenience tolerance and the budget have a stated order of precedence.** `--fewer-sims`
    measures its tolerance against the *unrestricted* cheapest total even when `--max-purchases`
    binds — if nothing inside the limit is within tolerance, the rule becomes `fewest_within_limit`
    and the report says so instead of claiming a percentage it missed. A budget outranks
    convenience (a convenient split that busts it is not bought) but a hard `--max-purchases` does
    not, so an over-budget recommendation under a limit is allowed and the budget line names the
    purchase count that *would* have fitted rather than claiming nothing exists. Every row of the
    ladder is also priced with the same currency clock (`vs_cheapest_pct` is null, not zero, when
    no floor exists), and the contiguous-window fallback says "fewest purchases found" because
    above 10 legs it does not search every split. Fixed after review; see DEVELOPMENT-LOG 2026-11-22.
11. **Installation happens where the internet is known.** `activate_lead_days`
    (`--activate-lead-days N`, default 0) moves every activation to N days before the earliest leg a
    plan serves — through one `_activation_day(legs)` helper used by local candidates, multi-leg
    candidates, strategies, diagnostics and the assumption text, so no site can forget it. It shifts
    **validity only**: data need is a sum over served legs and the lead-in day is not one of them.
    The consequence is measured and counter-intuitive — tight short-validity plans are the cheap glue
    of many-purchase splits, so a longer lead moves the optimum to *fewer* eSIMs (sample trip: 5 → 4
    → 3 as the lead goes 0 → 1 → 2). A negative activation day is valid output and renders as "the day
    before the trip starts".

## Known debt / open items

- Lint debt is swept. Re-run `ruff check .` before adding code; keep future sweeps out of
  feature diffs.
- `pyproject` declares a `live` extra (playwright) and a `live` pytest marker; **no live test
  exists**. Either write it or delete both. The comment now says so.
- **Licence is unchosen, and the repository is public.** `pyproject.toml` used to claim `MIT`
  with no LICENSE file anywhere; the claim is now removed (see DEVELOPMENT-LOG, 2026-11-11).
  With no licence, default copyright applies — anyone reading the public repo has no permission
  to reuse the code. If that is not intended, choose a licence and add the field *and* a LICENSE
  file together; do not re-add the field on its own.
- `itinerary.py` currency list and the wizard's currency list should not be able to drift; they
  are shared today but only by import.
- No cross-leg dedupe of identical SKUs: a strategy can buy the *same* multi-country plan
   once per leg (seen in a synthetic fixture: one plan bought three times; the `local_only`
   row and the frontier's `k = legs` row both do it). Buying two copies of an SKU is legal,
   so the search allows it, `_strategy_for_split` states it in the strategy's assumptions, and
   the ladder row is marked `buys one plan twice` (`buys_same_plan_twice` in `--json`) instead of
   leaving it silent. Whether the recommendation should *prefer* one copy over two — which needs
   SKU identity inside the partition DP, not just disclosure — is still open and untested.
- No CI, no packaging release flow, no screenshots/GIF in the README.

## Do not publish (ignored by design)

`itinerary.json` and any real trip file (personal travel plans), `.cache/` (acquired API
snapshots), `report.md` / `report.json` (generated), `.venv/`, all caches, `.env`, browser
profiles. Synthetic test payloads live inline in `tests/` and *are* published deliberately.
