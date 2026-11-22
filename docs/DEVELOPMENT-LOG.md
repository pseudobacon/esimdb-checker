# Development log

Dated, newest first. Compact design rationale is in [ARCHITECTURE.md](ARCHITECTURE.md);
the current-state snapshot is in [HANDOFF.md](HANDOFF.md).

---

## 2026-11-22 — The report had grown an audit trail; the answer went under it

The default console output carried everything: diagnostics, the speed-cap status block,
provenance/notes, snapshot hashes, a family table beside the ladder, per-strategy
verification assumptions, and a "Budget: none specified." line. The answer — the ladder and
the recommendation — was a minority of what was printed. The ask: the default output is the
answer; the audit trail is opt-in; the ladder becomes the boxed table; and the TUI shows the
results in place instead of dropping back to the shell.

- **`--verbose`** (shared output arg, both commands): console and Markdown skip diagnostics,
  speed-cap status, provenance/notes/snapshots, and the no-budget line by default; the flag
  restores them. JSON is deliberately untouched — it is the machine record, and pruning it
  would change the schema.
- **Verification assumptions are no longer printed** in console/Markdown (recommendation and
  `--show-purchases` rows alike). They remain in the JSON, and the speed-cap warning still sits
  on the plan's price line via `_plan_bits`, so a capped plan is never silently cleaner than it
  is. The now-unused `_strategy_detail_lines` helper was deleted.
- **The family table is gone** — redundant: every ladder rung says what it buys, and the new
  table carries a "What it buys" column (`_buys_summary`: one plan → provider + capacity/validity;
  a few → their names; local-only → "locals for all N stays"). The recommended rung is marked
  `▸` with a legend, because with `--fewer-sims`/`--max-purchases` the recommended rung is not
  always the cheapest one.
- **The ladder renders as a boxed table** (`_frontier_table`, hand-rolled box-drawing, fixed
  widths, deterministic for tests). The "Fewest that work" line stays above it. A recommendation
  with no budget set no longer prints "budget: within n/a".
- **The TUI stays open after the search.** `_search_finished` enters a `results` mode when the
  report has a ladder: the ladder page lists the rungs as options; `1..N` opens a detail page —
  a per-SIM table (plan, cost, activation, legs served) with every purchase link on its own line
  below the table (intact and copyable; an elided purchase link is a dead link) — `b`/`Esc` goes
  back, `q` exits with the completed result and the CLI prints the report. Error or
  nothing-feasible results leave immediately as before; Ctrl+C on the results pages closes the
  browser without overwriting the finished result with a cancellation.

Tests: the diagnostic-display assertions moved to `render_console(rep, verbose=True)` (they test
the audit trail, which is now verbose-only); the assumptions test was re-pointed at the data
side (`…_stay_in_the_data_not_the_output`); `tests/test_wizard_tui.py` gained two results-flow
tests driving the real app headlessly with a small real `Report` fixture (ladder → detail →
back → exit, Esc-back, stray keys inert).

`pytest` 212 passed (2 new TUI tests, 12 re-pointed), `ruff check .` clean, offline.

---

## 2026-11-22 — Flying days are half-days: `--transfer-data`

Same trip as the runs above. The model knew two kinds of day — a charged day (full daily need)
and a transfer day (zero) — so a flight day, which still uses some data, was either over-priced
or unpriced. The ask: flying days should charge half, and a plan that pays for a flying day must
actually be switched on and valid on it.

`--transfer-data F` (0..1, default 0, shared by `recommend` and `recommend-wizard`, and saved in
itinerary files) makes each transfer day cost F× the daily need:

- **Charging.** A leg's own pre-days ride on that leg (`required_gb = (days + T×F) × rate`);
  a 0-day leg — the layover shape `--stop 'China 0 +1'` — charges its share to the next
  destination leg, whose plan activates on those days, or to the previous one when it is the
  trip's tail, which must then stay valid through the layover. Every charged gigabyte has a
  responsible plan; nothing is left unowned.
- **Coverage.** With F > 0 the engine checks, per plan/leg-set: the plan must be active from
  the first charged pre-day (which is exactly what `--activate-lead-days 0` violates — such
  options are rejected instead of quietly under-covered) and valid through the last charged
  tail day (this is what kills a 30-day plan ending the day before a charged layover). F = 0
  keeps every existing report byte-identical.
- **Disclosure.** The header gains "flying days: 0.5x the daily data need", the per-leg table
  shows the charge, a provenance note explains it, and each affected plan states the flying
  days it also covers in its verification assumptions.
- **Rejected alternative:** fractional day slots (`Taiwan 3.7`). A flying day is a whole
  calendar day with partial use; fractional indices would break the integer day model that
  activation and validity checks are built on.

The 34-day scenario now runs as one precise command instead of a bracket:
`'Taiwan 2 +1' 'Indonesia 14 +1' 'Thailand 5 +1' 'China 4 +1' 'Taiwan 3 +1' 'China 0 +1'`
with `--transfer-data 0.5` = exactly 30 GB. The 2× Tripuck Asia 20 GB/30d answer (£13.98) is
confirmed under the exact numbers: first copy needs 17.5 GB of 19.5, second 13.5 of 19.5, and
the second's validity reaches day 34 for the layover.

`pytest` 210 passed (8 new offline tests: charging into requirements, a plan dying on a charged
tail day, the pool being charged not just the header, the pre-day/lead-0 interaction, CLI
plumbing and out-of-range rejection, file round-trip with override, wizard plumbing),
`ruff check .` clean, offline. F = 0 verified unchanged across the whole pre-existing suite.

---

## 2026-11-22 — The ladder priced six rows but explained one: `--show-purchases`

Same 32-day, four-country itinerary as the entry below it. The new table priced all six ways
to buy the trip, but only the recommended row carried an install list — so "what does the
2-eSIM row actually buy?" meant re-running the tool once per row and diffing the reports.

`--show-purchases N[,N…]` or `all` (on both `recommend` and `recommend-wizard`) expands the
named rows directly under the ladder, in ladder order:

- The requested counts are resolved against the frontier before rendering. A count no
  feasible option buys is exit 2 naming the counts that do — not a silent no-op, and not a
  shorter list pretending to be complete.
- `Report.ladder_strategies` holds the live `Strategy` per count for the renderers. It is
  deliberately not serialized: `to_dict()` is unchanged, because the JSON already carries the
  same data as `purchase_frontier[].plans` (including the per-purchase `activation_day`), and
  `totals.show_purchases` records what the text output expanded.
- The expanded block reuses the recommendation's install list through the shared
  `_buy_lines()`. An alternative you are asked to weigh must be described in exactly the same
  detail as the answer — plan, provider, install day, legs, price, data, validity — or the
  comparison is rigged by formatting.

**Then the loose ends from the handoff were closed.**

- **Expanded rows now carry the same verification assumptions as the answer.** Activation
  timing, validity span, and an accepted speed cap were printed only under RECOMMENDED, so a
  capped alternative was disclosed as a ⚠ bit on its plan line and nothing more. The expanded
  rows (console and Markdown) now print the same `verification assumptions:` block, so the
  "accepted despite a speed cap" rationale is visible on the row that buys the capped plan.
- **The wizard path is tested end-to-end.** `cmd_wizard` forwards the flag (it validates at
  entry and `_run_engine` resolves against the real frontier), which the tests now prove: the
  wizard scenario prices exactly one rung — two purchases of the same Europe plan, whole-trip
  infeasible on data — so `--show-purchases 2` expands with the "buys the same plan more than
  once" assumption stated, and `--show-purchases 1` exits 2 with no report printed. This was
  the flag's most likely failure mode: accepted by the wizard, silently ignored.
- **`all` was decided uncapped.** The capped diagnostics lists cap because they are a constant
  tax on every report; `all` is an explicit request, the compact ladder table directly above
  the blocks already shows how many rungs exist, and capping would reintroduce the per-row
  re-run the flag exists to remove. Recorded in the README next to the flag.
- **The JSON shape is documented** (README, "The JSON report"): `purchase_frontier` rows,
  `plans[].activation_day` (`0` = the day before the trip starts), `totals.show_purchases`,
  and `purchase_policy` (how the recommendation was chosen, including `budget_over_convenience`).

`pytest` 202 passed in 86.00s (3 new offline tests: assumptions on an expanded row, an
accepted cap disclosed on the row that buys it, the wizard forwarding the flag and refusing an
infeasible count), `ruff check .` clean, offline. Console, Markdown and JSON checked against
each other.

---

## 2026-11-22 — The price of convenience: cheapest strategy for every number of eSIMs

Same 32-day, four-country itinerary as the 2026-11-21 entry. That fix made the Simvito
Asia plan *visible* — priced under diagnostics, and usable in a hybrid — but the report
still recommended five separate eSIMs (£17.12) next to a whole-trip plan at £24.69, with
nothing in between. Asked why not one SIM, the honest answer was that the engine could
not price two.

The three families were three searches, and each search had a structural ceiling:
`local_only` buys one plan per leg, `whole_trip` buys exactly one plan, and `hybrid` buys
**at most one** multi-country plan plus singles. So the reachable purchase counts were 1,
*n*, and whatever count one regional plan happened to leave behind. A 2-eSIM answer of
two regional plans covering different halves of the trip — usually the right answer for a
long multi-country trip — had no code path at all, and no rung of the ladder was priced.

Replaced the family enumeration with a purchase-count search:

1. `_cover_costs` — cheapest plan per leg set, then a superset-min over all 2^n sets. The
   step rests on a monotonicity that holds for both feasibility rules: a plan proven on a
   set is proven on any subset, because the data need is a sum over the set (shrinks) and
   activating at the earliest leg of the subset can only leave *more* validity than
   activating earlier still.
2. `_partition_frontier` — a DP over set partitions keyed by exact purchase count. Taking
   the lowest leg of each set as that group's anchor visits every split once rather than
   k! times: 3^n states, ~3s end-to-end on a 6-leg trip.
3. `_strategy_for_split` — reconstructs each row into a `Strategy`, labelled `whole_trip`
   (k=1), `local_only` (k=legs) or `hybrid` (between), so the existing three-family report
   contract and every renderer survive unchanged.

Above `_MAX_PARTITION_LEGS = 10` legs the split search is restricted to contiguous travel
windows (`_window_frontier`, disclosed in the notes and `totals.partition_search`) rather
than attempting 3^n.

Measured on the real itinerary, same cache, before and after — the endpoints reproduce the
old engine exactly, which is what made the swap safe:

| eSIMs | Cheapest | vs cheapest | | |
| --- | --- | --- | --- | --- |
| 1 | £24.69 | +44.2% | Tripuck Asia 50 GB / 90 days | was `whole_trip` |
| 2 | £18.63 | +8.8% | Simvito Asia 30 GB/20d + Tripuck Asia 20 GB/30d | **was unreachable** |
| 3 | £17.19 | +0.4% | Asia 20 GB/30d + Indonesia + SE Asia | **was unreachable** |
| 4 | £17.21 | +0.5% | one Taiwan plan for both visits + three more | **was unreachable** |
| 5 | £17.12 | — | what it recommends | unchanged |
| 6 | £18.70 | +9.2% | one plan per destination | was `local_only` |

So three eSIMs cost 40p more than five, and two cost £1.51 more — the trade-off a reader
was being asked to guess at. It is now a table in all three report formats
(`## Cost of convenience`, `purchase_frontier` in JSON), with the chosen row marked.

Recommendation policy stayed purely cheapest, deliberately, with two opt-ins: `--max-purchases N`
(hard limit; when nothing fits it recommends nothing and names the fewest purchases that do,
rather than quietly overshooting) and `--fewer-sims [PCT]` (fewest eSIMs within a tolerance
of the cheapest total, default 10% — absolute-fewest is not the goal: the 1-eSIM option here
is 44% dearer and should lose). Verified: `--fewer-sims` → 2 eSIMs £18.63; `--fewer-sims 3
--max-purchases 4` → 3 eSIMs with both notes; `--max-purchases 1` → nothing recommended.

One judgement call rather than a fix: a split may buy the same SKU twice. Two copies of a
plan are two legal purchases with separate activations, so the search allows it, but a
strategy that does so now says so in its assumptions instead of listing the same eSIM twice
silently. Preferring one copy over two belongs inside the DP and is left open (HANDOFF).

`pytest` 185 passed in 80.14s (6 new offline tests: ladder endpoints equal the named
families, a 2-purchase split of two regional plans, an unsatisfiable purchase limit, the
tolerance choosing and refusing, contiguous-window fallback), `ruff check .` clean, offline,
~2.9s per run on the warm cache. Console, Markdown and JSON checked against each other.

### Then reviewed against real data — a policy that reported a tolerance it did not meet

A reviewer agent was pointed at the diff with real data. Three of its findings were real bugs,
and the third is the kind that makes an article about this engine wrong:

**1. `--max-purchases` redefined the floor for `--fewer-sims`.** The tolerance ceiling was
`min(cost in pool) * (1 + pct)`, and the pool was already filtered by the limit. So
`--fewer-sims 10 --max-purchases 1` computed 10% of £24.69 — which is £27.16, *above* the
£17.12 cheapest — and then labelled a **+44.2%** recommendation as "fewest eSIMs within 10% of
the cheapest total", in the header, the note, and the JSON (`tolerance_met: true`). The floor is
now always the unrestricted cheapest; when nothing inside a limit clears the tolerance, a
distinct rule `fewest_within_limit` takes over and the report says "fewest eSIMs the 1-eSIM limit
allows (none of them are within 10% of the cheapest total)" with `tolerance_met: false`.

**2. Budget and convenience had no defined order.** `--fewer-sims` picked a 2-eSIM split costing
£18.63 while a £17.12 split sat on the shelf; against a £18 budget the report said "EXCEEDED" and
"no strategy was found within your budget", and relaxing the budget to £19 was the only fix —
relaxing it to £18.70 would not have helped, because the cheap answer was always under £18. The
rule is now explicit: a budget outranks convenience, a hard `--max-purchases` outranks a budget
(the limit is the point of the flag), and when a limit forces an over-budget recommendation the
budget line names the purchase count that *would* have fitted — "A 3-purchase strategy at £17.19
would fit — a purchase policy is what ruled it out, not a lack of options".

**3. Two more claims the data did not support.** The contiguous-window fallback (trips above 10
legs) said "the fewest purchases that work on this trip", which is an optimality claim made by a
function that deliberately drops non-contiguous splits — now "fewest purchases found". And
`vs_cheapest_pct` uses `None` for "no floor exists", which `r.get(...) or 0.0` turned into a clean
0.0 read as "cheapest"; the console printed `cheapest` with no marker at all, so a reader could
not tell the two apart. Null is now handled explicitly in both renderers.

Smaller ones from the same review: a vacuous note ("the fewest eSIMs within 10% ...: 1 instead of
1") when the tolerance happened to pick the cheapest split; the ladder not saying when a row buys
one plan twice (now a footnote plus `buys_same_plan_twice` in `--json`); and dead state
(`RecommendationEngine._frontier`, unread since `_budget_relaxation` was removed) deleted. The
2^n cover table is also gone from the fallback path — window mode reads the sparse candidate map,
so a 15-leg trip no longer allocates 32768 entries only to decline to use them.

**Found while verifying: a documented flag path that had never worked.** `--budget` with
`--itinerary` raised `AttributeError: module 'esimdb_checker.fx' has no attribute 'convert'` —
`cmd_recommend` called a function that does not exist. Two more bugs hid behind it: `Trip` is
frozen, so the follow-up `trip.budget = converted` could never have run, and `budget_currency`
defaulted to the `--currency` default (EUR) rather than the itinerary's own currency, so a bare
`--budget 20` against a GBP trip invented a conversion nobody asked for. Fixed by defaulting an
unnamed budget currency to the trip's, adding `fx.convert_between` (USD as the intermediate,
returning None when a side is missing so the CLI can refuse rather than guess), and rebuilding
the frozen trip with `dataclasses.replace`. This is the exact command the README suggested, so the
documentation had to change with it.

`pytest` 192 passed in 82.57s (7 new offline tests: the limit-and-tolerance interaction, the
vacuous note, budget precedence including the over-budget-under-a-limit message, `--budget` on an
itinerary file in both currency modes, and `convert_between`), `ruff check .` clean. Re-checked on
the real itinerary: the ladder still reads 1 £24.69 / 2 £18.63 / 3 £17.19 / 4 £17.21 / 5 £17.12 /
6 £18.70, and the default recommendation is still 5 purchases at £17.12 — the fixes changed what
is *claimed*, not what is *chosen*. (Those are lead-0 numbers; the default lead is now 1.)

### Installing before you fly: the lead-in day buys *fewer* eSIMs

Asked whether the usual habit of activating an eSIM the day before arrival — because there may be
no internet on arrival to install it — changes the optimal combination. It was not modellable:
every activation in the engine happened exactly on the day of arrival (`min(leg.start_day)`), so
the question could not be asked of it. Added `activate_lead_days` (CLI `--activate-lead-days N`,
0 on arrival at the time — 1 by default now, see the next subsection), routed through one `_activation_day(legs)` helper
used by the local candidates, the multi-leg candidates, the strategies, the diagnostics and the
assumption text, so no site can forget it.

The mechanism is worth stating precisely: **the lead costs validity, not data.** `required_gb` is
a sum over the legs a plan serves, and the lead-in day is not one of them — you are not roaming
yet. What changes is `activation_day + period_days - 1 >= leg.end_day`, which now has to hold one
day earlier, so every purchase needs `N` more days of validity than the days it is on the road.

Measured on the real itinerary, same cache:

| lead | cheapest | ladder | |
| --- | --- | --- | --- |
| 0 | **5 eSIMs £17.12** | 1:24.69 2:18.63 3:17.19 4:17.21 5:17.12 6:18.70 | Taiwan 10 GB/**5 Days** £2.86 + SE Asia 1 day £0.87 |
| 1 | **4 eSIMs £17.21** | 1:24.69 2:18.63 3:17.41 4:17.21 5:17.32 6:18.90 | the 5-day plan cannot span 6 days, so the 30-day one (£3.82) replaces it |
| 2 | **3 eSIMs £17.41** | 1:24.69 2:19.01 3:17.41 4:17.51 5:17.51 6:19.09 | an Asia 20 GB/30d plan absorbs Taiwan, Thailand and China |

Two things fell out that no one would have guessed:

1. **The optimum moves the wrong way from intuition.** Pre-arrival activation does not push
   towards more eSIMs, it pushes towards fewer — because the plans that become invalid are the
   cheap tight ones (5 days for a 5-day visit) that make many-purchase splits cheap, while
   long-validity plans absorb a free extra day. The recommendation went 5 → 4 → 3 as the lead
   went 0 → 1 → 2.
2. **A knock-on the engine found, not the reasoning.** The 30-day Taiwan plan that replaces the
   dead 5-day one also spans the trip's *second* Taiwan visit (travel days 2 and 30), so it
   deletes the separate 1-day SE Asia plan too. The 4-purchase answer had been sitting at £17.21
   all along, 9p above the old winner; the lead day did not create it, it removed the thing that
   was beating it.

`--fewer-sims` is untouched by the lead: still 2 eSIMs at £18.63 at lead 0 and lead 1, same two
plans, activation days moved 2→1 and 21→20. The 90-day whole-trip plan (£24.69) has validity to
spare, so its price never moves — which is a useful sanity check that the lead shifts only what
it should.

Disclosure: the report header gains `activation: N days before the first arrival each plan
serves`, each plan line prints the install day (including travel day 0 / negative, rendered as
"the day before the trip starts"), each strategy carries the assumption in words, and a note
states that data need is deliberately unaffected. Negative `N` is rejected before any network
work.

`pytest` 195 passed in 80.23s (3 new offline tests: the 3→2 purchase flip with the `buys one plan
twice` row that only the degenerate split produces, the header/note disclosure with the default
off, and the CLI flag reaching the engine plus its rejection of negatives), `ruff check .` clean.

---

### The lead became the default: `--activate-lead-days 1`

Asked whether the day-before install should be the default: yes — it is what actually happens.
Installing needs internet, and arrival does not promise any. Flipping a default is a bigger change
than it looks, so the same commit does the three jobs that make a default safe to own:

- **The engine and the CLI share one constant** (`_DEFAULT_ACTIVATE_LEAD_DAYS`) instead of two
  literals that can drift apart. The constructor now distinguishes `None` (use the default) from
  an explicit `0` (install on arrival); the `int(x or 0)` it replaced collapsed the two, which is
  the same bug class as "`--max-purchases 0` is a no-op".
- **The default is asserted by a test**, so reverting it is a deliberate act rather than drift.
- **Every documented number was re-measured, none scaled.** The sample trip's default answer moved
  from 5 purchases £17.12 to 4 purchases £17.21, the ladder to 3:£17.41 / 5:£17.32 / 6:£18.90, and
  `--budget 18 --fewer-sims` from 5 purchases to 4. README, ARCHITECTURE and the HANDOFF checks
  block quote re-run values, and the `--max-purchases 1` line there is corrected too — it claimed
  "nothing recommended" when the 1-eSIM answer has been £24.69 all along.

Blast radius on the suite: two assertions in 195. `short_by_days` went 2 → 3 (a 30-day plan
installed the day before reaches travel day 29, not 30), and the lead-disclosure test was renamed
to assert the new default. Everything else passed untouched, because nearly every fixture gives
plans validity to spare — tight-fitting plans are exactly, and only, what a lead touches.

Recorded in README Limitations: the clock is assumed to start at **installation**, the pessimistic
reading. A provider that starts validity on first connection to a supported network makes
pre-installing free, and for those `--activate-lead-days 0` is the accurate setting. The engine
cannot tell which kind a plan is; the provider's terms can, and the assumption line is where to
look them up.

## 2026-11-21 — Multi-leg plans were invisible: candidate generation fixed

Reported against a real 32-day, four-country itinerary (Taiwan → Indonesia → Thailand →
China, back to Taiwan, back to China): the report recommended five single-destination
plans and never mentioned *Simvito Asia (12 destinations) 50 GB / 30 days* at £18.94, which
is a viable purchase for most of that trip. It was not ranked lower — it was never a
candidate at all.

Three separate causes, all in the multi-leg candidate pass (`_regional_candidates`):

1. **A plan was only tried against the set of legs it is listed on.** A plan listed on all
   four destination pages had to be data-feasible *and* valid for all four. A 30-day plan on
   a 32-day trip fails that by two days, so it was deleted before the subset enumeration in
   `_evaluate` could find the 24-day window it does serve. Feasibility is now judged per leg
   set the plan would actually be bought for, and that leg set (`frozenset`) is the dedupe
   key; `_servable_scopes` keeps only the *maximal* servable sets — widening a set only
   raises the data need and lengthens the span validity must reach, so every servable set
   sits under a maximal one, and the existing subset re-pricing in `_evaluate` covers the
   rest for free.
2. **Repeat visits were excluded outright** (`distinct countries >= 2`). The trip visits
   Taiwan twice and China twice, so it bought two China plans where one 7-day plan covers
   both stays (China legs are travel days 26-29 and 31-32). A leg set now needs >= 2 *legs*.
3. **The travel-day range ignored transfer days.** `total_days` sums charged days, and the
   report printed `1–{total_days}` — "31 travel day(s)" for a trip whose last leg runs to
   day 32. `Trip.span_days` (charged + transfer) is what the console header, the leg table
   and the Markdown table now print; `total_days` keeps its meaning and is labelled
   "charged". JSON gains `span_days` alongside `total_days`.

Visibility also improved, because "why wasn't X suggested?" was unanswerable: two new
diagnostic buckets in `_diagnostics` (console, JSON and Markdown share
`_diagnostics_lines`) — `regional_near_matches` (listed on >= 2 *different* countries,
Enough data, too short for the calendar; `legs_run_to_day` + `short_by_days`, sorted
shortest shortfall first) and `regional_alternatives` (feasible for some leg set but not
bought by any winning strategy; sorted most-legs-first then price). Same-country pairs are
deliberately out of the near-match bucket: a plan too short for both visits is still fine
for one, and per-leg picks already price it.

Measured on the same itinerary file, old code stashed and re-run (`asia-2026.itinerary.json`,
31 charged days over 32, 46.5 GB, GBP, cache warm):

```
before   header "31 travel day(s)"  local_only £18.70 (6) RECOMMENDED
         whole_trip £24.69 (1)      hybrid £19.56 (5)
         Simvito Asia 50GB/30d: 0 mentions anywhere in the report
after    header "31 charged day(s) over 32 travel day(s)", leg table 1–32
         local_only £18.70 (6)  whole_trip £24.69 (1)  hybrid £17.12 (5) RECOMMENDED
         one China plan serves both China stays; saves £1.58 vs local_only
         Simvito Asia 50GB/30d £18.93 first in regional_alternatives, with the legs it
         would cover: Taiwan+Indonesia+Thailand+China+Taiwan (days 2-30) and
         Indonesia+Thailand+China+Taiwan+China (days 6-32)
         diagnostics: 3,170 multi-country calendar misses, 47 multi-leg alternatives
```

Six tests added (`tests/test_recommend.py`): the 30-days-on-a-32-day-trip window, one plan
serving two visits to the same country, a calendar-only multi-country miss being named, a
feasible-but-not-bought multi-leg plan still being listed, the transfer-day span in the
rendered report, and a 10-leg trip taking (and disclosing) the contiguous-window search.
`pytest` 179 passed in 80.71s, `ruff check .` clean, offline. A run against the warm
cache is ~3s (was ~3s before); `regional_candidates_considered` is now one plan per leg set
(49 here) with `regional_candidates_truncated: 0`.

---

## 2026-11-21 — Published to GitHub

Repository: **https://github.com/pseudobacon/esimdb-checker** — owner `pseudobacon` (the
authenticated GitHub account, confirmed with `gh auth status` rather than inferred from the Git
identity), **visibility PUBLIC**, default branch `main`.

Sequence, because it matters for who did what: on 2026-11-11 the work was committed locally as
`a8a80fd` but could not be pushed (no credentials on the machine — recorded in the entry below).
The user then installed `gh`, authenticated, created the repository and pushed it themselves as
`48daa0f`, which also swept up the in-progress lint fixes. Two things followed here: the remaining
43 `E501` findings were finished as `5b6315e`, and the visibility the user had asked for
(private) differed from what had been created (public) — raised explicitly, and the user chose to
**keep it public**.

Post-push verification, run after the push and not before (state at the `5b6315e` push; a
later docs-only commit moved the branch tip on from it):

```
git ls-remote origin refs/heads/main   5b6315e50e207a96b9669984a01cf1207e8f31a7
git rev-parse HEAD                     5b6315e50e207a96b9669984a01cf1207e8f31a7   MATCH
gh repo view ... visibility            PUBLIC        defaultBranchRef main
push                                   48daa0f..5b6315e main -> main   (fast-forward, no force)
working tree                           clean; no untracked non-ignored files
```

Exclusions verified against **GitHub's own tree** (`git/trees/<sha>?recursive=1`, 31 blobs), not
the local index — `itinerary.json`, `.cache/`, `report.md`/`report.json`, `.venv/`,
`__pycache__/`, `.pytest_cache/`, `.ruff_cache/`, `.env` are each **absent**. The full six-commit
history is on the remote: `0f63cac → 41227f6 → f0988ff → a8a80fd → 48daa0f → 5b6315e`.

Nothing was deleted locally to achieve that: `itinerary.json` (644 B) and `.cache/` (14 MB) are
still on disk, merely ignored. No secrets were found in any blob in history, so no rewrite was
needed and none was performed.

Two consequences of "public, no licence", both recorded in `docs/HANDOFF.md`: default copyright
applies (no reuse rights granted), and the missing `LICENSE` is now more visible rather than more
urgent to invent — the licence field stays removed until a choice is made deliberately.

Also corrected here: this log and `docs/HANDOFF.md` were written on 2026-11-11 but the session
resumed on 2026-11-21, so the dates on the sweep entry and the current-state checks were wrong.

---

## 2026-11-21 — Lint sweep completed

`ruff check .` (rules `E,F,W,I,UP,B`, line length 100) had been failing with **174 findings at
the baseline commit**. It now reports no findings. Done in two passes:

1. **Mechanical** — `ruff check --fix`: 144 fixes, almost all `Optional[X]` → `X | None` under
   `from __future__ import annotations`, plus import order and unused imports.
2. **Judgement** — 8 findings autofix refuses to touch, checked individually rather than
   blind-fixed:
   - `F841 low = text.lower()` in `filters.speed_cap_reasons` — genuinely dead: every text
     pattern is compiled with `re.I` and searches `text`, so the lowered copy was never needed.
     Removed, not renamed.
   - 4 × `E741` ambiguous `l` → `leg` in `recommend.py`.
   - 3 × `B007` unused loop variables → `_pid` / `_i` / `_plan` (kept positional structure).
3. **Line length** — the remaining 43 `E501` lines wrapped by hand: signatures split over lines,
   long f-strings split into implicitly-concatenated literals, three ternaries parenthesised, one
   trailing comment moved above its statement. Two long inline expressions were pulled into named
   locals (`served = ...` in the strategy list, `provider = sp.provider if hasattr(...) else ...`
   in the buy list) rather than wrapped mid-expression.

**Why the wrapping is safe.** Re-rendered the same 5-country itinerary with the pre-sweep code
(a `git worktree` checkout of `HEAD`, forced onto `sys.path` with `PYTHONPATH`) and with the
post-sweep code, both against the same snapshot cache so no network was involved:

```
JSON     25168 B  identical
console   6849 B  identical apart from the generation timestamp
Markdown  6259 B  identical apart from the generation timestamp
```

The first attempt at that comparison was worthless and quietly so: running HEAD's CLI from a
worktree still imported the editable-installed package (cwd is not a package root), so both runs
executed the *new* code and "agreed". Caught by printing `esimdb_checker.__file__`; the real
comparison forces HEAD with `PYTHONPATH`.

An automated scan of every implicitly-concatenated literal pair also flagged no doubled or lost
space at a join.

`pytest`: 173 passed after the mechanical pass, and 173 passed again with the full sweep in
place; `ruff check .` exits 0.

**Not done here:** `--fix --unsafe-fixes` was never run; the two findings it would have made
(mostly removal of seemingly-unused code) were left for a human. Also note this entry was
committed *after* the repository had already been created and pushed — see the next entry.

---

## 2026-11-11 — Publication preparation

### State reviewed before touching anything

Three commits existed (`0f63cac` baseline → `41227f6` → `f0988ff`), on branch `master`, with
**no remote configured** and 12 files modified but uncommitted (the interactive wizard and the
speed-cap/data-need refinement work). `itinerary.json` was the only untracked file — and it
holds real destinations and day counts, so it was never staged.

`AGENTS.md` and `docs/HANDOFF.md`, referenced by the working brief, did not exist and were not
in any commit; this change creates `docs/HANDOFF.md`.

### Security review

Scanned the worktree (tracked + untracked text files) **and every blob in the full history**
for API keys, secrets, passwords, private keys, and GitHub/Anthropic token shapes.
**No matches.** No `.env`, no stored credentials, no cookies, no browser profiles in the tree.
No history rewrite was needed or performed.

### Exclusions hardened

`.gitignore` gained explicit rules for the things a research tool accumulates but must never
publish: local itineraries (`itinerary.json`, `*.itinerary.json`, `itineraries/`, `trips/`),
generated reports (`report.md`, `report.json`, `*.report.*`), environment files and key material
(`.env`, `.env.*` with `!.env.example`, `*.pem`, `*.key`), browser profiles/session data, and
debug scratch (`*.log`, `*.out`, `scratch/`, `tmp/`).

Deliberately **not** ignored: `*.json` in general. Verified afterwards that a test fixture at
`tests/fixtures/keep.json` is still trackable, and that `git ls-files -i -c` is empty — i.e. no
already-tracked file was silenced by the new rules. Nothing was deleted from disk; the local
itinerary, `.cache/` snapshots and reports remain in place, merely untracked.

### Documentation

- **README**: test count corrected (it claimed 142; the suite is 173) and the duplicated
  suite-description paragraph merged; added an explicit **"No browser is required"** statement
  under Install (plus Python 3.10+); added a **Limitations** section covering research-only
  scope, undocumented upstream endpoints, coverage inherited from the site, the fact that speed
  caps are a *local predicate* (`verified: false`) rather than the site's toggle, averaging in
  the data model, ECB reference rates vs real charges, and what is not modelled at all
  (taxes, device bands, locks, tethering/voice policy, registration requirements); added an
  honest note that `ruff check .` currently fails with pre-existing style findings.
- **`pyproject.toml`**: corrected a false claim — the comment said playwright is "used only by
  the opt-in live smoke test", but no test carries the `live` marker and nothing imports
  playwright. The comment now states the extra is aspirational.
- **`docs/HANDOFF.md`** (new): compact current-state snapshot.
- **`docs/DEVELOPMENT-LOG.md`** (new): this file.

### Checks actually run

```
python -m pytest tests/ -p no:cacheprovider   173 passed in 88.55s        (pass)
python -m ruff check .                         175 findings, exit 1       (fail)
python -m mypy --version                       module not installed       (n/a)
```

The ruff failure is pre-existing style debt, not a regression: the same command on the
`HEAD` tree extracted to a clean directory reports **174** findings. It was left unfixed so the
publication diff stays reviewable; the sweep belongs in its own commit.

`mypy` is not configured, so no type check was claimed.

### Branch

Renamed `master` → `main` (only branch, no remote, no other refs to disturb) so the fresh
repository starts on `main`.

### Publishing: blocked at the time, then unblocked by the user

**At the time this entry was written, the repository had not been created and nothing had been
pushed.** This machine had no GitHub credentials of any kind:

```
gh                     command not found
GH_TOKEN/GITHUB_TOKEN  unset
~/.config/gh           absent
~/.git-credentials     absent      ~/.netrc absent
ssh -T git@github.com  Permission denied (publickey)   (no keys in ~/.ssh)
```

Authentication is required before any repository creation or push. Until it succeeds, no
commit hash, URL, or visibility should be recorded as the published state.
