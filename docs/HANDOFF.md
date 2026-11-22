# HANDOFF — current state

Compact snapshot. Design detail lives in [ARCHITECTURE.md](ARCHITECTURE.md); the dated
history lives in [DEVELOPMENT-LOG.md](DEVELOPMENT-LOG.md).

Snapshot date: **2026-11-22**.

## What this is

A local, keyboard-driven CLI that reads esimdb.com's own JSON endpoints and recommends a
whole-itinerary eSIM purchase strategy (local / whole-trip / hybrid) against destinations,
day counts, data need, validity and budget. Research only — it never buys or activates.

## Current state

| | |
| --- | --- |
| Package | `esimdb-checker` 0.1.0, `src/` layout, Python 3.10+, console script `esimdb-checker` |
| Runtime deps | `httpx`, `textual` — **no browser, no Playwright at runtime** |
| Tests | **212 passing**, fully offline (respx-mocked API, Textual headless Pilot) |
| Lint | `ruff check .` **clean, exit 0**. Pre-existing debt (174 findings at the baseline commit) swept in two passes — see DEVELOPMENT-LOG. |
| Type check | none configured (`mypy` not installed, no config) |
| CI | none |
| Publishing | Published at `https://github.com/pseudobacon/esimdb-checker` — **public** (chosen deliberately), default branch `main`, `origin` points there |

## Checks that were actually run (2026-11-22, re-run after review)

```
python -m pytest tests/ -p no:cacheprovider   -> 195 passed in 82.74s
python -m ruff check .                        -> All checks passed (exit 0)
esimdb-checker recommend --itinerary asia-2026.itinerary.json    (default install lead = 1 day)
     -> Cost of convenience: 1 £24.69 / 2 £18.63 / 3 £17.41 / 4 £17.21 / 5 £17.32 / 6 £18.90
     -> default recommendation: 4 purchases £17.21; header carries
        "activation: 1 day before the first arrival each plan serves"
esimdb-checker recommend … --activate-lead-days 0
     -> 1 £24.69 / 2 £18.63 / 3 £17.19 / 4 £17.21 / 5 £17.12 / 6 £18.70, 5 purchases £17.12
        (a LONGER install lead buys FEWER eSIMs; see DEVELOPMENT-LOG 2026-11-22)
esimdb-checker recommend … --fewer-sims        -> 2 eSIMs £18.63 (+8.2%, inside the 10% tolerance)
esimdb-checker recommend … --max-purchases 2   -> 2 eSIMs £18.63; --max-purchases 1 -> 1 eSIM £24.69
esimdb-checker recommend … --fewer-sims 3 --max-purchases 4 -> 3 eSIMs £17.41
esimdb-checker recommend … --fewer-sims 10 --max-purchases 1 -> rule fewest_within_limit,
     tolerance_met false, header "fewest eSIMs the 1-eSIM limit allows" (no false 10% claim)
esimdb-checker recommend … --budget 18         -> 4 purchases £17.21, within budget (path used to crash)
esimdb-checker recommend … --budget 17.5 --max-purchases 2
     -> "A 3-purchase strategy at £17.41 would fit - a purchase policy is what ruled it out"
esimdb-checker recommend … --budget 18 --fewer-sims -> 4 purchases: the 2-eSIM split busts £18
esimdb-checker recommend … --budget 17 --fewer-sims -> 2 eSIMs £18.63; nothing on the ladder fits £17
esimdb-checker recommend … --show-purchases 3 -> prints the 3-eSIM buy list under the ladder,
     recommendation unchanged (2 eSIMs £18.63 with --fewer-sims)
esimdb-checker recommend … --show-purchases all -> one block per ladder row (6 on the sample trip)
esimdb-checker recommend … --show-purchases 7 -> exit 2: "no feasible option buys [7]; the
     purchase counts that do exist are [1, 2, 3, 4, 5, 6]"
esimdb-checker recommend … --show-purchases two -> exit 2: "takes 'all' or purchase counts"
esimdb-checker recommend … --show-purchases 2,3 --md -> "**What 2 eSIMs would buy**" bullets
# 34-day scenario with six flying days charged at half (exact, no brackets):
esimdb-checker recommend --stop 'Taiwan 2 +1' --stop 'Indonesia 14 +1' --stop 'Thailand 5 +1' \
  --stop 'China 4 +1' --stop 'Taiwan 3 +1' --stop 'China 0 +1' \
  --daily-data 1 --currency GBP --transfer-data 0.5 --show-purchases all
     -> 28 charged days over 34 travel days, 31 GB (28 full + six half); ladder
        1 £24.90 / 2 £13.73 / 3 £12.22 / 4 £11.64 / 5 £10.99 <- recommended
     -> flying-day assumptions list days 1, 4, 19, 25, 30, 34 per serving plan
esimdb-checker recommend --itinerary …        -> console, --json, --md, --strict-speed-cap, --include-capped
pty-driven wizard walkthrough                 -> see README "Verified in a terminal"
```

## `--show-purchases` — completed 2026-11-22

Started and committed 2026-11-22 (`895f6c8`), loose ends closed the same day — this section now
records the finished state, not open work. It answers a real complaint: the ladder priced six
ways to buy the trip while only the recommendation said what you would install, so weighing the
3-eSIM row meant re-running the tool once per row.

What exists: `--show-purchases N[,N…]|all` in `_add_engine_args` (so `recommend` and
`recommend-wizard` both accept it); `_parse_show_purchases` for syntax; counts resolved against
`report.purchase_frontier` at the end of `_run_engine` and echoed into
`report.totals["show_purchases"]` in ladder order; `Report.ladder_strategies` (the live strategy
per row, deliberately absent from `to_dict()`); `report._expanded_rows()` for console and an
equivalent block in `render_markdown`; `report._buy_lines()` factored out of the recommendation
block so both renderers share one implementation; `activation_day` added to each
`purchase_frontier[].plans` entry in `--json`. Expanded rows print the same
`verification assumptions:` block as the recommendation (activation timing, validity span, an
accepted speed cap) in both renderers.

The five loose ends from the first commit, all closed:

1. **Assumptions on expanded rows** — added under the buy list in console and Markdown; a
   speed-capped alternative is no longer disclosed only via the `⚠` in `_plan_bits`.
2. **Wizard path tested end-to-end** (`tests/test_cli_wizard.py`) — the flag reaches the report
   through the interview, and a count no strategy buys exits 2 with no report printed.
3. **DEVELOPMENT-LOG entry written** (2026-11-22, "The ladder priced six rows but explained one").
4. **`all` decided uncapped** — an explicit request (unlike the capped diagnostics lists), the
   compact ladder table above the blocks already shows the rung count, and capping would
   reintroduce the per-row re-run the flag removes. Recorded in the README next to the flag.
5. **JSON shape documented** — README "The JSON report": `purchase_frontier` rows,
   `plans[].activation_day`, `totals.show_purchases`, `purchase_policy`.

Verified: full suite offline — **212 tests, no failures, exit 0** — and `ruff check .` clean
(run as `python -m pytest tests/ -p no:cacheprovider -q`; `pyproject.toml` also sets
`addopts = "-q"`, and in a redirected run the "N passed" summary line was absent while every dot
was printed — check the exit code, not the last line).

## Output pruning, `--verbose`, boxed ladder, TUI results page — completed 2026-11-22

The report had grown an audit trail (diagnostics, speed-cap status, provenance/notes,
snapshots, per-strategy verification assumptions, a family table beside the ladder) that
buried the answer. The user's rule: the default output is the answer; the audit trail is
opt-in.

What changed, all in one commit:

- **`--verbose`** (shared output arg, `recommend` + `recommend-wizard`): console and Markdown
  now skip the itinerary parameter bullets, diagnostics, speed-cap status,
  provenance/notes/snapshots, and the "Budget: none specified." line by default;
  `--verbose` restores them. JSON is always complete — it is the machine record, and
  pruning it would change the schema.
- **Verification assumptions are no longer printed** in console/Markdown (recommendation
  block and `--show-purchases` rows alike); they stay in the JSON (`strategies.*.assumptions`,
  `purchase_frontier` plans) and the speed-cap warning still sits on the plan's detail line
  via `_plan_bits`.
- **The family comparison table is gone** — redundant: every ladder rung already says what it
  buys, and the new table carries a "What it buys" column (`_buys_summary`: single plan →
  provider + capacity/validity; multi-plan rungs → their shape, `regional + local`,
  `2 locals + regional` — regional = one plan spanning several legs — not a string of
  product names; local-only → "locals for all N stays"). The recommended rung is marked `▸`
  with a legend line.
- **The ladder is now a boxed table** (`_frontier_table`, hand-rolled box-drawing, fixed
  column widths) — the same text the TUI shows. The "Fewest that work" line stays above it.
  Buy-list plans are one dotted line each (serves · price · data · days), and the old long
  "Itinerary:" summary line is now bullet points under `--verbose` (the legs table stays
  in the default output).
- **The TUI no longer exits after the search.** `_search_finished` enters a `results` mode
  when the report has a ladder: the ladder page (same boxed table, rungs listed as options at
  the bottom), `1..N` opens a detail page — a rich table per SIM (plan, cost, activation,
  serves) with the purchase links on their own lines below (intact and copyable, never
  elided) — `b`/`Esc` back, `q` exits with the completed result (the CLI then prints the
  report). Error/no-feasible results leave immediately, as before. Ctrl+C on the results
  pages closes the browser without overwriting the finished result with a cancellation.

Tests: diagnostic-display assertions moved to `render_console(rep, verbose=True)`; the
assumptions test was re-pointed at the JSON-side data (`test_expanded_row_assumptions_stay_in_the_data_not_the_output`);
`tests/test_wizard_tui.py` gained two results-flow tests driving the real app headlessly
(ladder → detail → back → exit, with a small real `Report` fixture).

## Where things live

- `src/esimdb_checker/cli.py` — argparse surface; `recommend`, `recommend-wizard`, `plans`,
  `countries`, `resolve`, `fetch`. Shared engine flags in `_add_engine_args`.
- `recommend.py` — acquisition, candidate generation, the purchase-count frontier
  (`_cover_costs` → `_partition_frontier`), policies (`_choose`), diagnostics, totals.
- `filters.py` — `speed_cap_signals` (always-throttled vs after-allowance), `purchasable`,
  `meets_data_need_for_days`.
- `wizard.py` — Textual TUI + `--plain` fallback; both build the same `Trip`.
- `report.py` — console / JSON / Markdown. `render_console`/`render_markdown` take `verbose`
  (default off: the answer; verbose: the audit trail). `_diagnostics_lines()` is shared by
  console and Markdown so the two renderers cannot disagree; `_frontier_table()` is the boxed
  ladder the console and the TUI both show.
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
6. **A plan's page set is not its servable set.** Multi-leg candidates are generated per
   leg set the plan would actually be bought for, not per the legs it happens to be listed
   on: feasibility is re-checked per set, only maximal servable sets are kept as dedupe
   keys, and >= 2 **legs** (not countries) qualifies — so repeat visits share one plan and a
   plan too short for the whole trip can still serve a window of it. Rejected multi-leg
   plans are explained by `regional_near_matches` / `regional_alternatives` rather than
   vanishing. See DEVELOPMENT-LOG 2026-11-21.
7. **The families are labels over one search, not three searches.** The engine prices every
   purchase count (1 eSIM … one per leg) and calls k=1 `whole_trip`, k=legs `local_only`, the
   rest `hybrid`. They used to be enumerated separately, which meant a 2-eSIM answer was
   unpriceable and often unfindable — the search bought at most one multi-country plan. Cheapest
   stays the default recommendation; `--max-purchases` (hard limit) and `--fewer-sims [PCT]`
   (fewest within a tolerance, default 10%) are opt-in, and a limit nothing fits recommends
   nothing rather than quietly exceeding it. A split may buy the same SKU twice; that is legal
   and says so in the assumptions. See DEVELOPMENT-LOG 2026-11-22.
8. **Provenance is part of the record.** Snapshots, SHA-256, fetch times and the FX rate are in
   every report's JSON, and `speed_filter_status.mode` always states `local_predicate` vs
   `verified_site_toggle` honestly. In the human output they are the audit trail: shown with
   `--verbose`, not in the default answer (see the 2026-11-22 section above).
9. **The convenience tolerance and the budget have a stated order of precedence.** `--fewer-sims`
    measures its tolerance against the *unrestricted* cheapest total even when `--max-purchases`
    binds — if nothing inside the limit is within tolerance, the rule becomes `fewest_within_limit`
    and the report says so instead of claiming a percentage it missed. A budget outranks
    convenience (a convenient split that busts it is not bought) but a hard `--max-purchases` does
    not, so an over-budget recommendation under a limit is allowed and the budget line names the
    purchase count that *would* have fitted rather than claiming nothing exists. Every row of the
    ladder is also priced with the same currency clock (`vs_cheapest_pct` is null, not zero, when
    no floor exists), and the contiguous-window fallback says "fewest purchases found" because
    above 10 legs it does not search every split. Fixed after review; see DEVELOPMENT-LOG 2026-11-22.
10. **Installation happens where the internet is known.** `activate_lead_days`
    (`--activate-lead-days N`, **default 1**, asserted by a test so it cannot drift; 0 reproduces
    the pre-2026-11 numbers) moves every activation to N days before the earliest leg a
    plan serves — through one `_activation_day(legs)` helper used by local candidates, multi-leg
    candidates, strategies, diagnostics and the assumption text, so no site can forget it. It shifts
    **validity only**: data need is a sum over served legs and the lead-in day is not one of them.
    The consequence is measured and counter-intuitive — tight short-validity plans are the cheap glue
    of many-purchase splits, so a longer lead moves the optimum to *fewer* eSIMs (sample trip: 5 → 4
    → 3 as the lead goes 0 → 1 → 2). A negative activation day is valid output; the human report
    no longer prints per-plan activation lines (the header states the lead, the JSON and the
    strategy assumptions carry the exact day).

11. **An alternative must be described in the same detail as the answer.** `--show-purchases`
    expands "Cost of convenience" rows into install lists using `_buy_lines()`, the same helper the
    recommendation uses, so the two cannot drift in wording or detail — and the expanded rows now
    carry the same `verification assumptions:` block (activation timing, validity span, an accepted
    speed cap), so a capped alternative is not disclosed less honestly than the answer. It is
    reporting, not choosing: nothing about which strategy is recommended moves. Rows are named by
    purchase count, validated against the rows that actually exist, and an impossible count exits 2
    naming the feasible ones rather than printing nothing. `Report.ladder_strategies` carries the
    live strategies and stays out of `to_dict()` because `purchase_frontier` already publishes that
    information. `all` is deliberately uncapped (explicit request; the ladder table shows the rung
    count). See DEVELOPMENT-LOG 2026-11-22 and the "completed" section above.
12. **A flying day is a whole calendar day with partial use — and its data must have an owner.**
    `--transfer-data F` charges each transfer day F× the daily need: a leg's pre-days ride on that
    leg, a tail 0-day leg rides on the previous leg, which must then stay valid through the layover.
    With F > 0 the engine rejects any plan/leg-set not active on a charged pre-day (exactly what
    `--activate-lead-days 0` breaks) or not valid through a charged tail day. Fractional day slots
    (`Taiwan 3.7`) were rejected: they would break the integer day model activation and validity are
    built on. Default 0 keeps every existing report identical; the fraction is saved in itinerary
    files and round-trips. See DEVELOPMENT-LOG 2026-11-22 ("Flying days are half-days").
13. **The default report is the answer, not the audit trail.** Console/Markdown print the
    itinerary, the boxed ladder (with a what-it-buys column and a `▸` on the recommended rung),
    the named buy lists, and the recommendation. Diagnostics, speed-cap status, provenance,
    snapshots, and the no-budget line come back with `--verbose`; the verification assumptions
    are never printed anymore — they live in the JSON, where a machine can check them. The
    per-family table was deleted as redundant with the ladder. The TUI shows the same ladder
    in place after the search, with each rung openable to a per-SIM detail page (cost,
    activation, purchase link) and back/exit keys.

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
