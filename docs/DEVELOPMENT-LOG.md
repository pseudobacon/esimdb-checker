# Development log

Dated, newest first. Compact design rationale is in [ARCHITECTURE.md](ARCHITECTURE.md);
the current-state snapshot is in [HANDOFF.md](HANDOFF.md).

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

### NOT done: publishing

**The repository has not been created and nothing has been pushed.** This machine has no
GitHub credentials of any kind:

```
gh                     command not found
GH_TOKEN/GITHUB_TOKEN  unset
~/.config/gh           absent
~/.git-credentials     absent      ~/.netrc absent
ssh -T git@github.com  Permission denied (publickey)   (no keys in ~/.ssh)
```

Authentication is required before any repository creation or push. Until it succeeds, no
commit hash, URL, or visibility should be recorded as the published state.
