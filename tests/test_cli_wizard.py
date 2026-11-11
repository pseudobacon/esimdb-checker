"""Offline tests for the CLI: shared output options, wizard wiring, currency handling.

The interactive wizard is driven through a fake TTY stdin so the real
`input()`-based plain front-end runs, and every network call is mocked with an
isolated `--data-dir`.
"""

from __future__ import annotations

import io
import json
import sys

import pytest
from conftest import (
    FakeStream,
    build_plan_entry,
    build_plans_payload,
    make_provider,
    mock_fx_route,
    mock_geo_routes,
    mock_plans_route,
)

from esimdb_checker.cli import _build_parser, main

REPORT_MARKER = "eSIM strategy recommendation"
P1 = make_provider("P1", "TestSim", "testsim", 500)

# Answers for the plain wizard: spain 4 + france 7, EUR, budget 200, 2 GB/day,
# search now, skip saving.
WIZARD_ANSWERS = "spain\n4\n\nfrance\n7\n\n\n\n200\n2\ny\n\n"


def payload() -> dict:
    return build_plans_payload([
        build_plan_entry(
            plan_id="eu-20", provider=P1, name="Europe 20GB/30Days",
            capacity_mb=20000, period_days=30,
            prices={"USD": 25.0, "EUR": 24.0, "GBP": 19.0},
        )
    ])


def fake_tty(monkeypatch, stdin_text: str = "", out_tty: bool = True, err_tty: bool = True):
    """Install fake stdin/stdout/stderr with controllable isatty(); return (out, err)."""
    out, err = FakeStream("", tty=out_tty), FakeStream("", tty=err_tty)
    monkeypatch.setattr(sys, "stdin", FakeStream(stdin_text, tty=True))
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", err)
    return out, err


def wizard_args(tmp_path, *extra) -> list[str]:
    # --plain keeps these CLI tests deterministic: the real TUI needs a real terminal.
    return ["--data-dir", str(tmp_path), "recommend-wizard", "--refresh", "--plain", *extra]


def rc_of(argv) -> int:
    """Run main(argv) and report its exit code, whether it returns or raises SystemExit."""
    try:
        return main(argv)
    except SystemExit as exc:
        return exc.code


# ---------------------------------------------------------------------------
# Shared output options
# ---------------------------------------------------------------------------
def test_recommend_and_wizard_share_output_and_engine_options():
    parser = _build_parser()
    def opts(command):
        action = parser._subparsers._group_actions[0].choices[command]  # noqa: SLF001
        return {s for a in action._actions for s in a.option_strings}  # noqa: SLF001
    rec, wiz = opts("recommend"), opts("recommend-wizard")
    for flag in ("--json", "--md", "--out", "--provider", "--include-capped",
                 "--refresh", "--currency", "--daily-data", "--budget", "--itinerary"):
        assert flag in rec, f"recommend missing {flag}"
        assert flag in wiz, f"recommend-wizard missing {flag}"


def test_json_and_md_are_mutually_exclusive(tmp_path, monkeypatch):
    out, err = fake_tty(monkeypatch, WIZARD_ANSWERS)
    with pytest.raises(SystemExit) as exc:
        main(wizard_args(tmp_path, "--json", "--md"))
    assert exc.value.code == 2
    assert "cannot be combined" in err.getvalue()


def test_out_requires_a_nonempty_path(tmp_path, monkeypatch):
    out, err = fake_tty(monkeypatch, WIZARD_ANSWERS)
    with pytest.raises(SystemExit) as exc:
        main(wizard_args(tmp_path, "--out", " "))
    assert exc.value.code == 2
    assert "non-empty file path" in err.getvalue()


# ---------------------------------------------------------------------------
# Wizard -> report through every output mode
# ---------------------------------------------------------------------------
def test_wizard_console_report_printed_exactly_once(tmp_path, monkeypatch):
    out, err = fake_tty(monkeypatch, WIZARD_ANSWERS)
    with _mocked(tmp_path):
        rc = main(wizard_args(tmp_path))
    assert rc == 0
    text = out.getvalue()
    assert text.count(REPORT_MARKER) == 1, text
    assert "\x1b" not in text                       # plain mode: no escape sequences
    assert "Currency for prices and budget" in err.getvalue()   # questions stay in the wizard


def test_wizard_json_stdout_is_pure_json(tmp_path, monkeypatch):
    # stdout is a pipe: the interview must move to stderr and stdout must parse.
    out, err = fake_tty(monkeypatch, WIZARD_ANSWERS, out_tty=False)
    with _mocked(tmp_path, fx="EUR"):
        rc = main(wizard_args(tmp_path, "--json"))
    assert rc == 0
    data = json.loads(out.getvalue())                # raises if anything polluted stdout
    assert set(data["strategies"]) == {"local_only", "whole_trip", "hybrid"}
    assert data["itinerary"]["total_days"] == 11
    assert data["itinerary"]["currency"] == "EUR"
    assert data["recommended"]["plans"][0]["plan"] == "Europe 20GB/30Days"
    assert "Currency for prices and budget" in err.getvalue()


def test_wizard_json_can_also_be_copied_to_a_file(tmp_path, monkeypatch):
    target = tmp_path / "nested" / "report.json"
    out, err = fake_tty(monkeypatch, WIZARD_ANSWERS, out_tty=False)
    with _mocked(tmp_path):
        rc = main(wizard_args(tmp_path, "--json", "--out", str(target)))
    assert rc == 0
    assert json.loads(target.read_text())["itinerary"]["total_days"] == 11
    assert json.loads(out.getvalue())                 # stdout stays pure JSON
    assert "json report written to" in err.getvalue()


def test_wizard_writes_markdown_and_stays_off_stdout(tmp_path, monkeypatch):
    target = tmp_path / "reports" / "trip.md"
    out, err = fake_tty(monkeypatch, WIZARD_ANSWERS)
    with _mocked(tmp_path):
        rc = main(wizard_args(tmp_path, "--md", "--out", str(target)))
    assert rc == 0
    md = target.read_text()
    assert md.startswith("# eSIM Strategy Recommendation")
    assert "Spain" in md and "France" in md
    assert REPORT_MARKER not in out.getvalue()        # the report body is not on stdout
    assert f"wrote {target}" in err.getvalue()


def test_wizard_writes_console_text_to_out(tmp_path, monkeypatch):
    target = tmp_path / "trip.txt"
    out, err = fake_tty(monkeypatch, WIZARD_ANSWERS)
    with _mocked(tmp_path):
        rc = main(wizard_args(tmp_path, "--out", str(target)))
    assert rc == 0
    assert REPORT_MARKER in target.read_text()
    assert REPORT_MARKER not in out.getvalue()


def test_wizard_output_path_failure_is_a_clean_error(tmp_path, monkeypatch):
    blocker = tmp_path / "afile"
    blocker.write_text("not a directory")
    out, err = fake_tty(monkeypatch, WIZARD_ANSWERS)
    with pytest.raises(SystemExit) as exc:
        main(wizard_args(tmp_path, "--md", "--out", str(blocker / "sub" / "r.md")))
    assert exc.value.code == 2
    body = err.getvalue()
    assert "could not write output file" in body
    assert "Traceback" not in body


def test_wizard_saves_the_itinerary_file(tmp_path, monkeypatch):
    saved = tmp_path / "itinerary.json"
    fake_tty(monkeypatch, WIZARD_ANSWERS)
    with _mocked(tmp_path):
        rc = main(wizard_args(tmp_path, "--json", "--save-file", str(saved)))
    assert rc == 0
    doc = json.loads(saved.read_text())
    assert [s["country"] for s in doc["stops"]] == ["Spain", "France"]
    assert doc["currency"] == "EUR" and doc["daily_data_gb"] == 2


def test_wizard_cancelled_is_a_nonzero_exit(tmp_path, monkeypatch):
    fake_tty(monkeypatch, "spain\n4\n\n\n\n\n\nn\n")  # finish, currency, budget, daily, 'n'
    out = sys.stdout
    with _mocked(tmp_path):
        rc = main(wizard_args(tmp_path, "--json"))
    assert rc == 2
    assert "cancelled" in sys.stderr.getvalue()
    assert out.getvalue() == ""


def test_global_data_dir_is_honoured_by_every_command(tmp_path):
    """`--data-dir` before the subcommand must not be clobbered by a subparser default."""
    parser = _build_parser()
    for argv in (
        ["--data-dir", "/tmp/x", "recommend", "spain"],
        ["--data-dir", "/tmp/x", "recommend-wizard"],
        ["--data-dir", "/tmp/x", "plans", "spain"],
        ["--data-dir", "/tmp/x", "countries"],
        ["--data-dir", "/tmp/x", "resolve", "IT"],
    ):
        assert parser.parse_args(argv).data_dir == "/tmp/x", argv


def test_wizard_plain_flag_is_honoured(tmp_path, monkeypatch):
    out, err = fake_tty(monkeypatch, WIZARD_ANSWERS)
    with _mocked(tmp_path):
        rc = main(wizard_args(tmp_path, "--plain"))
    assert rc == 0
    assert "plain mode" in err.getvalue()
    assert REPORT_MARKER in out.getvalue()


def _broken_tui_run(self):
    raise RuntimeError("no terminal available")


def test_tui_failure_falls_back_to_the_plain_wizard(tmp_path, monkeypatch):
    """A TUI that cannot run must degrade to plain mode, not crash the user's shell."""
    import esimdb_checker.wizard as wz
    monkeypatch.setattr(wz.WizardApp, "run", _broken_tui_run)
    out, err = fake_tty(monkeypatch, WIZARD_ANSWERS)   # all three streams claim to be TTYs
    with _mocked(tmp_path):                            # no --plain: the TUI is chosen first
        rc = main(["--data-dir", str(tmp_path), "recommend-wizard", "--refresh"])
    assert rc == 0
    assert "could not run" in err.getvalue()
    assert "falling back to the plain wizard" in err.getvalue()
    assert REPORT_MARKER in out.getvalue()


@pytest.mark.parametrize("stdin_tty,stdout_tty,stderr_tty,json_mode,plain_flag,expected", [
    (True, True, True, False, False, True),     # a real terminal gets the TUI
    (True, True, True, False, True, False),     # --plain opts out
    (True, True, True, True, False, False),     # --json: stdout must stay clean
    (True, False, True, False, False, False),   # piped stdout -> plain
    (True, True, False, False, False, False),   # redirected stderr -> plain
    (False, True, True, False, False, False),   # no stdin: rejected earlier anyway
])
def test_tui_is_only_chosen_for_a_real_terminal(stdin_tty, stdout_tty, stderr_tty,
                                                json_mode, plain_flag, expected):
    from esimdb_checker.wizard import wants_tui
    assert wants_tui(stdin_tty, stdout_tty, stderr_tty, json_mode, plain_flag) is expected


def test_noninteractive_stdin_explains_the_alternative(tmp_path, capsys):
    # Under pytest stdin is not a TTY and --plain was not requested: fail helpfully
    # instead of hanging on a front-end that cannot read answers.
    assert rc_of(["--data-dir", str(tmp_path), "recommend-wizard", "--refresh"]) == 2
    err = capsys.readouterr().err
    assert "needs an interactive terminal" in err
    assert "recommend --stop" in err


def test_piped_answers_are_read_when_plain_is_requested(tmp_path, monkeypatch, capsys):
    """`--plain` explicitly asks for the line interview, so a pipe of answers is honoured."""
    monkeypatch.setattr(sys, "stdin", io.StringIO(WIZARD_ANSWERS))
    with _mocked(tmp_path):
        rc = main(["--data-dir", str(tmp_path), "recommend-wizard",
                   "--refresh", "--plain"])
    assert rc == 0
    captured = capsys.readouterr()
    assert "reading the interview answers from stdin" in captured.err
    assert REPORT_MARKER in captured.out


def test_a_short_pipe_ends_the_interview_without_a_traceback(tmp_path, monkeypatch, capsys):
    """EOF mid-interview is a clean cancellation, never a traceback."""
    monkeypatch.setattr(sys, "stdin", io.StringIO("spain\n4\n"))
    with _mocked(tmp_path):
        rc = main(["--data-dir", str(tmp_path), "recommend-wizard",
                   "--refresh", "--plain"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "no more input available" in err
    assert "Traceback" not in err


# ---------------------------------------------------------------------------
# Currency: flags, wizard choices, saved configs
# ---------------------------------------------------------------------------
def test_cli_currency_flag_sets_the_report_currency(tmp_path, monkeypatch):
    out, _ = fake_tty(monkeypatch, "spain\n4\n\n\n\n\n\ny\n\n")
    with _mocked(tmp_path, fx="GBP"):
        rc = main(wizard_args(tmp_path, "--json", "--currency", "GBP"))
    assert rc == 0
    data = json.loads(out.getvalue())
    assert data["itinerary"]["currency"] == "GBP"
    assert data["fx"]["currency"] == "GBP"


def test_wizard_interactive_currency_choice_wins_over_the_default(tmp_path, monkeypatch):
    out, _ = fake_tty(monkeypatch, "spain\n4\n\n\nGBP\n\n\ny\n\n")
    with _mocked(tmp_path, fx="GBP"):
        rc = main(wizard_args(tmp_path, "--json", "--currency", "EUR"))
    assert rc == 0
    assert json.loads(out.getvalue())["itinerary"]["currency"] == "GBP"


def test_invalid_cli_currency_is_rejected_at_parse_time(tmp_path):
    with pytest.raises(SystemExit) as exc:
        main(["--data-dir", str(tmp_path), "recommend", "spain", "--days", "4",
              "--currency", "15"])
    assert exc.value.code == 2


def test_unknown_cli_currency_is_rejected_with_the_supported_list(tmp_path, capsys):
    with pytest.raises(SystemExit):
        main(["--data-dir", str(tmp_path), "recommend", "spain", "--days", "4",
              "--currency", "XYZ"])
    assert "is not a supported currency" in capsys.readouterr().err


def test_lowercase_cli_currency_is_normalized(tmp_path, monkeypatch):
    out, _ = fake_tty(monkeypatch, "")
    with _mocked(tmp_path, fx="JPY"):
        rc = main(["--data-dir", str(tmp_path), "recommend", "spain", "--days", "4",
                   "--currency", "jpy", "--json", "--refresh"])
    assert rc == 0
    assert json.loads(out.getvalue())["itinerary"]["currency"] == "JPY"


# ---------------------------------------------------------------------------
# Reusing a saved itinerary from the command line
# ---------------------------------------------------------------------------
def _saved(tmp_path, currency="EUR", stops=None, daily=1.5):
    path = tmp_path / "itinerary.json"
    path.write_text(json.dumps({
        "stops": stops or [
            {"country": "Spain", "country_slug": "spain", "days": 4, "transfer_days": 0},
            {"country": "France", "country_slug": "france", "days": 7, "transfer_days": 0},
        ],
        "daily_data_gb": daily,
        "currency": currency,
    }), encoding="utf-8")
    return path


def test_recommend_reuses_a_saved_itinerary(tmp_path, capsys):
    path = _saved(tmp_path, currency="USD", daily=2)
    with _mocked(tmp_path, fx="USD"):
        rc = main(["--data-dir", str(tmp_path), "recommend", "--itinerary", str(path),
                   "--json", "--refresh"])
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert data["itinerary"]["currency"] == "USD"
    assert data["itinerary"]["total_days"] == 11
    assert abs(data["itinerary"]["required_gb"] - 22.0) < 1e-9


def test_recommend_flags_override_a_saved_itinerary(tmp_path, capsys):
    path = _saved(tmp_path, currency="USD", daily=2)
    with _mocked(tmp_path, fx="EUR"):
        rc = main(["--data-dir", str(tmp_path), "recommend", "--itinerary", str(path),
                   "--currency", "EUR", "--daily-data", "1", "--json", "--refresh"])
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert data["itinerary"]["currency"] == "EUR"
    assert abs(data["itinerary"]["required_gb"] - 11.0) < 1e-9


def test_saved_itinerary_with_numeric_currency_reports_an_explanation(tmp_path, capsys):
    path = _saved(tmp_path, currency="15")
    # No respx mock here: the file is validated offline, before any network call.
    assert rc_of(["--data-dir", str(tmp_path), "recommend", "--itinerary", str(path)]) == 1
    err = capsys.readouterr().err
    assert "invalid currency '15'" in err
    assert "looks like a number" in err
    assert "Traceback" not in err


def test_wizard_rejects_a_broken_saved_itinerary_before_the_interview(tmp_path, monkeypatch):
    path = _saved(tmp_path, currency="15")
    out, err = fake_tty(monkeypatch, "")
    assert rc_of(wizard_args(tmp_path, "--itinerary", str(path), "--json")) == 1
    assert "invalid currency '15'" in err.getvalue()
    assert "Destination" not in err.getvalue()      # the interview never started


def test_wizard_starts_from_a_saved_itinerary(tmp_path, monkeypatch):
    path = _saved(tmp_path, currency="USD", daily=2)
    # Loaded legs are shown; answers: finish, currency EUR, no budget, keep daily, search.
    out, err = fake_tty(monkeypatch, "\nEUR\n\n\ny\n")
    with _mocked(tmp_path, fx="EUR"):
        rc = main(wizard_args(tmp_path, "--json", "--itinerary", str(path)))
    assert rc == 0
    data = json.loads(out.getvalue())
    assert [s["country"] for s in data["itinerary"]["legs"]] == ["Spain", "France"]
    assert data["itinerary"]["currency"] == "EUR"
    assert "USD" in err.getvalue()                    # the loaded currency was shown


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
class _mocked:
    """respx-mock geo + FX + one country's plans for a tmp data dir."""

    def __init__(self, tmp_path, fx="EUR", rate=0.92):
        self.tmp_path, self.fx, self.rate = tmp_path, fx, rate
        self._ctx = None

    def __enter__(self):
        import respx
        self._ctx = respx.mock(assert_all_called=False)
        router = self._ctx.__enter__()
        mock_geo_routes(router)
        mock_fx_route(router, self.fx, self.rate)
        for slug in ("spain", "france", "japan"):
            mock_plans_route(router, slug, payload())
        return router

    def __exit__(self, *exc):
        return self._ctx.__exit__(*exc)


# --------------------------------------------------------------------------
# the TUI runs the search itself: the CLI must emit that report, not repeat it
# --------------------------------------------------------------------------
def test_a_report_from_the_tui_is_not_searched_twice(tmp_path, monkeypatch):
    import esimdb_checker.wizard as wz
    from esimdb_checker import cli
    seen = {}

    def fake_tui(geo, state, save_path=None, overwrite=False, search=None):
        seen["search"] = search
        return wz.WizardResult(trip=object(), cancelled=False, report="REPORT")

    def must_not_run_again(*a, **k):
        raise AssertionError("the engine ran twice")

    fake_tty(monkeypatch)
    monkeypatch.setattr(wz, "run_tui_wizard", fake_tui)
    monkeypatch.setattr(cli, "_run_engine", must_not_run_again)
    monkeypatch.setattr(cli, "_emit",
                        lambda report, args, err=None: seen.update(emitted=report))
    with _mocked(tmp_path):
        rc = main(["--data-dir", str(tmp_path), "recommend-wizard", "--refresh"])
    assert rc == 0
    assert seen["emitted"] == "REPORT"
    assert callable(seen["search"])          # the interface was handed the query


def test_a_search_failure_inside_the_tui_exits_with_an_error(tmp_path, monkeypatch):
    import esimdb_checker.wizard as wz
    from esimdb_checker import cli

    out, err = fake_tty(monkeypatch)
    monkeypatch.setattr(wz, "run_tui_wizard", lambda *a, **k: wz.WizardResult(
        trip=object(), cancelled=False, search_error="esimdb.com unreachable"))
    monkeypatch.setattr(cli, "_run_engine",
                        lambda *a, **k: pytest.fail("must not search after a failure"))
    with _mocked(tmp_path):
        with pytest.raises(SystemExit) as exc:
            main(["--data-dir", str(tmp_path), "recommend-wizard", "--refresh"])
    assert exc.value.code == 1
    assert "esimdb.com unreachable" in err.getvalue()
    assert REPORT_MARKER not in out.getvalue()
