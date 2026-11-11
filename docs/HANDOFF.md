# HANDOFF — current state

Compact snapshot. Design detail lives in [ARCHITECTURE.md](ARCHITECTURE.md); the dated
history lives in [DEVELOPMENT-LOG.md](DEVELOPMENT-LOG.md).

Snapshot date: **2026-11-11**.

## What this is

A local, keyboard-driven CLI that reads esimdb.com's own JSON endpoints and recommends a
whole-itinerary eSIM purchase strategy (local / whole-trip / hybrid) against destinations,
day counts, data need, validity and budget. Research only — it never buys or activates.

## Current state

| | |
| --- | --- |
| Package | `esimdb-checker` 0.1.0, `src/` layout, Python 3.10+, console script `esimdb-checker` |
| Runtime deps | `httpx`, `textual` — **no browser, no Playwright at runtime** |
| Tests | **173 passing**, fully offline (respx-mocked API, Textual headless Pilot) |
| Lint | `ruff check .` **fails: ~175 findings** — 174 already present at the baseline commit. Style debt (`UP045` ~102, `E501` ~44), not behaviour failures. Not yet swept. |
| Type check | none configured (`mypy` not installed, no config) |
| CI | none |
| Publishing | **not yet pushed to GitHub** — no credentials exist on this machine (see DEVELOPMENT-LOG) |

## Checks that were actually run (2026-11-11)

```
python -m pytest tests/ -p no:cacheprovider   -> 173 passed in 88.55s
python -m ruff check .                        -> FAILED, 175 findings (174 pre-existing at HEAD)
esimdb-checker recommend --itinerary …        -> console, --json, --md, --strict-speed-cap, --include-capped
pty-driven wizard walkthrough                 -> see README "Verified in a terminal"
```

## Where things live

- `src/esimdb_checker/cli.py` — argparse surface; `recommend`, `recommend-wizard`, `plans`,
  `countries`, `resolve`, `fetch`. Shared engine flags in `_add_engine_args`.
- `recommend.py` — acquisition, candidate generation, the three families, diagnostics, totals.
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
6. **Provenance is part of the output.** Snapshots, SHA-256, fetch times and the FX rate are in
   every report; `speed_filter_status.mode` always states `local_predicate` vs
   `verified_site_toggle` honestly.

## Known debt / open items

- Sweep the ~175 ruff findings as a dedicated commit (keep it out of feature diffs).
- `pyproject` declares a `live` extra (playwright) and a `live` pytest marker; **no live test
  exists**. Either write it or delete both. The comment now says so.
- **Licence is unchosen.** `pyproject.toml` used to claim `MIT` with no LICENSE file anywhere;
  the claim is now removed (see DEVELOPMENT-LOG, 2026-11-11). Choose a licence and add the field
  *and* a LICENSE file together — do not re-add the field on its own.
- `itinerary.py` currency list and the wizard's currency list should not be able to drift; they
  are shared today but only by import.
- No CI, no packaging release flow, no screenshots/GIF in the README.

## Do not publish (ignored by design)

`itinerary.json` and any real trip file (personal travel plans), `.cache/` (acquired API
snapshots), `report.md` / `report.json` (generated), `.venv/`, all caches, `.env`, browser
profiles. Synthetic test payloads live inline in `tests/` and *are* published deliberately.
