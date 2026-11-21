# Development log

Dated, newest first. Compact design rationale is in [ARCHITECTURE.md](ARCHITECTURE.md);
the current-state snapshot is in [HANDOFF.md](HANDOFF.md).

---

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
