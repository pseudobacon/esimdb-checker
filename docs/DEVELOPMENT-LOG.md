# Development log

Dated, newest first. Compact design rationale is in [ARCHITECTURE.md](ARCHITECTURE.md);
the current-state snapshot is in [HANDOFF.md](HANDOFF.md).

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
