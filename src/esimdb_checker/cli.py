"""Command-line interface."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import httpx

from . import config, http
from .catalog import fetch_country_plans
from .fx import validate_currency_code
from .geo import Geo
from .itinerary import (
    CountryNotFoundError,
    StopError,
    build_trip,
    load_itinerary_file,
    load_itinerary_trip,
    parse_stops,
    save_itinerary_file,
)
from .models import Trip
from .recommend import RecommendationEngine
from .report import render_console, render_json, write_report


# ---------------------------------------------------------------------------
# Argument construction
# ---------------------------------------------------------------------------
def _currency_arg(raw: str) -> str:
    """argparse type: validate a --currency value against the supported set."""
    code, err = validate_currency_code(raw)
    if err is not None:
        raise argparse.ArgumentTypeError(err)
    return code


def _add_output_args(p: argparse.ArgumentParser) -> None:
    """Shared output options for `recommend` and `recommend-wizard`."""
    p.add_argument("--json", action="store_true",
                   help="print the full typed report as JSON to stdout")
    p.add_argument("--md", action="store_true",
                   help="write a Markdown report (to --out, default report.md)")
    p.add_argument("--out", default=None, metavar="PATH",
                   help="output file for --md (or the console text when --md/--json are absent)")


def _add_engine_args(p: argparse.ArgumentParser) -> None:
    """Shared acquisition / filtering options."""
    p.add_argument("--provider", default=None,
                   help="restrict to one provider (name or slug)")
    p.add_argument("--include-capped", action="store_true",
                   help="include speed-capped plans (overrides the uncapped requirement)")
    p.add_argument("--strict-speed-cap", action="store_true",
                   help="also exclude plans throttled only after their allowance is spent "
                        "(the pre-2026-08 behaviour); by default those are kept, since a "
                        "plan that covers the trip's data need never reaches the throttle")
    p.add_argument("--refresh", action="store_true",
                   help="re-fetch from esimdb.com, bypassing the cache")


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="esimdb-checker",
        description="Research travel eSIM purchases on esimdb.com and recommend a "
                    "complete purchase strategy for an itinerary.",
    )
    p.add_argument("--data-dir", help="override the snapshot/data cache directory")
    sub = p.add_subparsers(dest="command", required=True)

    rec = sub.add_parser("recommend", help="recommend a complete eSIM strategy for an itinerary")
    rec.add_argument("destinations", nargs="*",
                     help="one destination (combine with --days); empty when using "
                          "--stop/--itinerary")
    rec.add_argument(
        "--stop",
        action="append",
        metavar="'<destination> <days>'",
        help="per-leg entry, repeatable; e.g. --stop 'Spain 4' --stop 'France 7' "
             "(use empty quotes '' for an uncharged transfer leg; "
             "append ' +N' for N transfer days)",
    )
    rec.add_argument("--itinerary", default=None, metavar="PATH",
                     help="load a saved itinerary file (from recommend-wizard) instead of --stop")
    rec.add_argument("--days", type=int, default=None,
                     help="days for a single destination (when not using --stop)")
    rec.add_argument("--budget", type=float, default=None,
                     help="total trip budget (in --budget-currency, default --currency)")
    rec.add_argument("--budget-currency", default=None, type=_currency_arg,
                     help="currency the budget is expressed in (defaults to --currency)")
    rec.add_argument("--currency", default=None, type=_currency_arg,
                     help="requested ranking/report currency (default EUR)")
    rec.add_argument("--daily-data", dest="daily_data", type=float, default=None,
                     help="data GB per destination day (default 1.5)")
    _add_engine_args(rec)
    _add_output_args(rec)
    rec.add_argument("--save-itinerary", default=None, metavar="PATH",
                     help="save the resolved itinerary to this file (JSON)")
    rec.add_argument("--overwrite", action="store_true",
                     help="allow --save-itinerary/--save-file to overwrite an existing file")
    rec.set_defaults(func=cmd_recommend)

    wiz = sub.add_parser("recommend-wizard",
                         help="interactive itinerary wizard (TUI), then recommendation")
    # NOTE: --data-dir is a global flag only. Redeclaring it here would let the
    # subparser's default silently overwrite a `--data-dir` given before the
    # subcommand (which would send snapshots to the project cache instead).
    wiz.add_argument("--itinerary", default=None, metavar="PATH",
                     help="start from a saved itinerary file (edit it interactively)")
    wiz.add_argument("--currency", default=None, type=_currency_arg,
                     help="default currency offered by the wizard (default EUR)")
    wiz.add_argument("--budget", type=float, default=None,
                     help="pre-fill the total trip budget (in --currency)")
    wiz.add_argument("--daily-data", dest="daily_data", type=float, default=None,
                     help="pre-fill data GB per destination day (default 1.5)")
    wiz.add_argument("--plain", action="store_true",
                     help="use the line-oriented wizard instead of the in-place TUI")
    wiz.add_argument("--save-file", default=None, metavar="PATH",
                     help="save the confirmed itinerary to this file (JSON)")
    wiz.add_argument("--overwrite", action="store_true",
                     help="allow --save-file to overwrite an existing file")
    _add_engine_args(wiz)
    _add_output_args(wiz)
    wiz.set_defaults(func=cmd_wizard)

    plans = sub.add_parser("plans",
                           help="list purchasable, non-speed-capped plans for a destination")
    plans.add_argument("destination", help="destination name, ISO code, or slug")
    plans.add_argument("--limit", type=int, default=20, help="max plans to show (default 20)")
    plans.add_argument("--include-capped", action="store_true",
                       help="include speed-capped plans (labeled)")
    plans.add_argument("--currency", default=None, type=_currency_arg,
                       help="convert prices to this currency")
    plans.add_argument("--refresh", action="store_true")
    plans.set_defaults(func=cmd_plans)

    countries = sub.add_parser("countries", help="list countries known to esimdb.com")
    countries.add_argument("--region", default=None, help="filter by region slug")
    countries.add_argument("--query", default=None, help="substring filter on name/code/slug")
    countries.add_argument("--refresh", action="store_true")
    countries.set_defaults(func=cmd_countries)

    resolve = sub.add_parser("resolve", help="resolve a destination token to a canonical country")
    resolve.add_argument("token", help="name, ISO code, or slug")
    resolve.add_argument("--refresh", action="store_true")
    resolve.set_defaults(func=cmd_resolve)

    fetch = sub.add_parser("fetch",
                           help="fetch one URL into the snapshot cache and show its metadata")
    fetch.add_argument("url", help="https URL")
    fetch.add_argument("--refresh", action="store_true")
    fetch.set_defaults(func=cmd_fetch)

    return p


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _data_dir(args) -> Path:
    return Path(args.data_dir) if getattr(args, "data_dir", None) else config.data_dir()


def _client() -> httpx.Client:
    return http.build_client()


def _run_engine(client: httpx.Client, trip: Trip, args, data_dir: Path, use_cache: bool,
                progress=None):
    engine = RecommendationEngine(
        client,
        data_dir,
        use_cache=use_cache,
        include_capped=getattr(args, "include_capped", False),
        strict_speed_cap=getattr(args, "strict_speed_cap", False),
        provider=getattr(args, "provider", None),
    )
    engine.load(trip, progress=progress)
    return engine.recommend(trip)


def _validate_output_args(args) -> None:
    """Reject impossible output combinations before any interview or network work."""
    wants_json = bool(getattr(args, "json", False))
    wants_md = bool(getattr(args, "md", False))
    out = getattr(args, "out", None)
    if wants_json and wants_md:
        _fail("--json and --md cannot be combined (JSON goes to stdout, --md writes a file)", 2)
    if out is not None and not str(out).strip():
        _fail("--out requires a non-empty file path", 2)


def _emit(report, args, err=None) -> None:
    """Emit the final report exactly once, and never mix prose into JSON stdout.

    --json  -> full typed report on stdout (optionally also copied to --out)
    --md    -> Markdown written to --out (default report.md); confirmation on stderr
    --out   -> console text written to that file
    else    -> console text on stdout

    Output-path problems are reported as a clean error (exit 2), never a traceback.
    """
    if err is None:
        err = sys.stderr            # resolved at call time so redirection works
    _validate_output_args(args)
    wants_json = bool(getattr(args, "json", False))
    wants_md = bool(getattr(args, "md", False))
    out = getattr(args, "out", None)

    try:
        if wants_json:
            text = render_json(report)
            if out:  # copy to a file while keeping stdout pure JSON
                path = Path(out)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text + "\n", encoding="utf-8")
                print(f"json report written to {path}", file=err)
            print(text)
            return
        if wants_md:
            path = Path(out) if out else Path("report.md")
            _write(path, lambda: write_report(report, path, "markdown"))
            print(f"wrote {path}", file=err)
            return
        if out:
            path = Path(out)
            _write(path, lambda: write_report(report, path, "console"))
            print(f"wrote {path}", file=err)
            return
        print(render_console(report))
    except OSError as e:
        _fail(f"could not write output file: {e}", 2)


def _write(path: Path, do_write) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    do_write()


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------
def cmd_recommend(args) -> int:
    _validate_output_args(args)
    if args.itinerary:
        # Cheap, offline validation first: a broken saved file is reported before
        # any network work.
        try:
            load_itinerary_file(Path(args.itinerary))
        except StopError as e:
            _fail(str(e))
    data_dir = _data_dir(args)
    use_cache = not args.refresh
    currency = args.currency or "EUR"
    budget_currency = args.budget_currency or currency
    budget = args.budget
    daily_data = args.daily_data if args.daily_data is not None else 1.5

    client = _client()
    with client:
        geo = Geo.load(client, data_dir, use_cache)

        try:
            if args.itinerary:
                if args.stop or args.destinations:
                    _fail("--itinerary cannot be combined with --stop or positional destinations")
                trip = load_itinerary_trip(
                    Path(args.itinerary), geo,
                    daily_data_gb=args.daily_data, currency=args.currency, budget=args.budget,
                )
            else:
                stops = _stops_from_args(args, geo)
                trip = build_trip(stops, daily_data_gb=daily_data, currency=currency, budget=budget)
        except CountryNotFoundError as e:
            print(f"error: {e}", file=sys.stderr)
            return 3
        except StopError as e:
            _fail(str(e))

        # convert a budget given in another currency so ranking/budget agree
        if budget is not None and budget_currency != trip.currency:
            from . import fx as fx_mod

            fx = fx_mod.fetch_fx(client, trip.currency, data_dir, use_cache)
            converted = fx_mod.convert(budget, budget_currency, trip.currency, fx)
            if converted is None:
                _fail(f"could not convert budget from {budget_currency} to "
                      f"{trip.currency} (FX unavailable)")
            trip.budget = converted

        if args.save_itinerary:
            saved = save_itinerary_file(trip, Path(args.save_itinerary), overwrite=args.overwrite)
            print(f"saved itinerary to {saved}", file=sys.stderr)

        report = _run_engine(client, trip, args, data_dir, use_cache)
    _emit(report, args)
    return 0


def _stops_from_args(args, geo):
    """Resolve --stop entries or a single positional destination + --days."""
    if args.stop:
        if args.destinations:
            _fail("--stop and positional destinations cannot be combined; use only --stop")
        return parse_stops(args.stop, geo)
    if args.destinations:
        if len(args.destinations) == 1:
            c = geo.resolve_country(args.destinations[0])
            if c is None:
                raise CountryNotFoundError(f"unrecognized country: {args.destinations[0]!r}")
            if args.days is not None:
                return [(c, args.days, 0)]
            _fail("provide --days <N> with a single destination, "
                  "or --stop entries for per-leg days")
        _fail(
            f"{len(args.destinations)} destinations need per-leg days; "
            "use --stop '<name> <days>' for each (e.g. --stop 'Spain 4' --stop 'France 7')"
        )
    _fail(
        "no destination given (provide one destination with --days, "
        "--stop entries, or --itinerary)"
    )


def cmd_wizard(args) -> int:
    _validate_output_args(args)
    from . import wizard as wz

    data_dir = _data_dir(args)
    use_cache = not args.refresh
    json_mode = bool(getattr(args, "json", False))

    stdin_tty = sys.stdin.isatty()
    stdout_tty = sys.stdout.isatty()
    stderr_tty = sys.stderr.isatty()

    if not stdin_tty and not args.plain:
        print(
            "error: recommend-wizard needs an interactive terminal, but stdin is not a TTY.\n"
            "  Run it directly in a terminal, or build the same itinerary non-interactively:\n"
            "    esimdb-checker recommend --stop 'Spain 4' --stop 'France 7' --currency EUR\n"
            "    esimdb-checker recommend --itinerary itinerary.json\n"
            "  (To keep the saved itinerary only: "
            "esimdb-checker recommend --itinerary FILE --json)",
            file=sys.stderr,
        )
        return 2
    if not stdin_tty:
        # `--plain` is an explicit request for the line-oriented interview, so a pipe
        # or file of answers is honoured; the interview ends cleanly at EOF.
        print("note: stdin is not a terminal - reading the interview answers from stdin "
              "(EOF ends the interview).",
              file=sys.stderr)

    if args.itinerary:
        try:
            load_itinerary_file(Path(args.itinerary))   # offline validation before the interview
        except StopError as e:
            _fail(str(e))

    # When stdout carries machine-readable or redirected output, the interview must
    # not pollute it: talk on stderr instead, and fall back to the plain front-end.
    ui_on_stderr = json_mode or not stdout_tty
    use_tui = wz.wants_tui(stdin_tty, stdout_tty, stderr_tty, json_mode, args.plain)

    client = _client()
    with client:
        geo = Geo.load(client, data_dir, use_cache)
        try:
            if args.itinerary:
                state = wz.state_from_file(Path(args.itinerary), geo)
            else:
                state = wz.WizardState()
            if args.currency:
                ok, err = state.set_currency(args.currency)
                if not ok:
                    _fail(err)
            if args.daily_data is not None:
                ok, err = state.set_daily_data(args.daily_data)
                if not ok:
                    _fail(err)
            if args.budget is not None:
                state.budget = args.budget
        except CountryNotFoundError as e:
            print(f"error: {e}", file=sys.stderr)
            return 3
        except StopError as e:
            _fail(str(e))

        save_path = Path(args.save_file) if args.save_file else None
        report = None
        if use_tui:
            print("eSIM itinerary wizard — starting the in-place interface (Esc cancels; "
                  "it will not print into the report).", file=sys.stderr)
            # Hand the query to the interface: it runs in a worker thread so the screen
            # stays live and shows which destination it is waiting on.
            search = lambda trip, progress: _run_engine(  # noqa: E731
                client, trip, args, data_dir, use_cache, progress=progress)
            try:
                result = wz.run_tui_wizard(geo, state, save_path=save_path,
                                           overwrite=args.overwrite, search=search)
            except Exception as e:  # terminal quirk, Textual unavailable, etc.
                print(f"  the in-place interface could not run ({type(e).__name__}: {e}); "
                      "falling back to the plain wizard.", file=sys.stderr)
                result = wz.run_plain_wizard(
                    geo, state, ask=lambda: input(), out=lambda s="": print(s, file=sys.stderr),
                    save_path=save_path, overwrite=args.overwrite,
                )
        else:
            if ui_on_stderr and not args.plain:
                print("(stdout is not an interactive screen — using the plain wizard; "
                      "its questions are on stderr so stdout can carry the report.)",
                      file=sys.stderr)
            result = wz.run_plain_wizard(
                geo, state,
                ask=lambda: input(),
                out=lambda s="": print(s, file=sys.stderr),
                save_path=save_path,
                overwrite=args.overwrite,
            )

        if result.cancelled or result.trip is None:
            print("wizard cancelled — no itinerary searched.", file=sys.stderr)
            return 2

        if result.search_error:
            # The interface already showed the failure; report it in the shell's format.
            _fail(result.search_error)

        if result.report is not None:
            report = result.report            # searched inside the TUI, nothing to redo
        else:
            print("Searching esimdb.com for this itinerary ...", file=sys.stderr)
            report = _run_engine(client, result.trip, args, data_dir, use_cache)
    _emit(report, args)
    return 0


def cmd_plans(args) -> None:
    client = _client()
    with client:
        data_dir = _data_dir(args)
        use_cache = not args.refresh
        geo = Geo.load(client, data_dir, use_cache)
        c = geo.resolve_country(args.destination)
        if c is None:
            _fail(f"unknown destination: {args.destination}")
        from . import fx as fx_mod

        currency = args.currency or "EUR"
        fx = fx_mod.fetch_fx(client, currency, data_dir, use_cache)
        plans, _ = fetch_country_plans(client, c.slug, data_dir, use_cache)
        rows = []
        for p in plans:
            price, comparable = p.price_in(currency, fx)
            rows.append(
                {
                    "name": p.name,
                    "provider": p.provider,
                    "_price": price,
                    "price": (
                        f"{price:g} {currency}" + ("" if comparable else " (uncomparable)")
                        if price is not None
                        else "n/a"
                    ),
                    "data": p.capacity_label(),
                    "period": p.period_label(),
                    "speed_cap": p.data_cap_label() or "",
                }
            )
        rows.sort(key=lambda r: (r["_price"] is None, r["_price"] or 0.0))
        _print_table(
            ["plan", "provider", "price", "data", "period", "speed_cap"],
            [[r["name"], r["provider"], r["price"], r["data"], r["period"], r["speed_cap"]]
             for r in rows[: args.limit]],
        )


def cmd_countries(args) -> None:
    client = _client()
    with client:
        geo = Geo.load(client, _data_dir(args), not args.refresh)
        countries = geo.countries
        if args.region:
            codes = geo.region_codes_for(args.region)
            countries = [c for c in countries if c.code in codes]
        if args.query:
            q = args.query.lower()
            countries = [
                c
                for c in countries
                if q in c.name.lower() or q in c.code.lower() or q in c.slug
            ]
        for c in sorted(countries, key=lambda c: (-c.popularity, c.name)):
            print(f"{c.code}\t{c.name}\t{c.slug}")


def cmd_resolve(args) -> None:
    client = _client()
    with client:
        geo = Geo.load(client, _data_dir(args), not args.refresh)
        c = geo.resolve_country(args.token)
    if c is None:
        _fail(f"unknown destination: {args.token}")
    print(f"{c.name}\t{c.code}\t{c.slug}\t{c.region or ''}")


def cmd_fetch(args) -> None:
    client = _client()
    with client:
        data_dir = _data_dir(args)
        _, meta = http.fetch_json(client, args.url, data_dir, not args.refresh)
    print(json.dumps(meta, indent=2))


def _print_table(headers: list[str], rows: list[list[str]]) -> None:
    if not rows:
        print("(no rows)")
        return
    widths = [len(h) for h in headers]
    for r in rows:
        for i, cell in enumerate(r):
            widths[i] = max(widths[i], len(str(cell)))
    line = "  ".join(h.ljust(widths[i]) for i, h in enumerate(headers))
    print(line)
    print("  ".join("-" * w for w in widths))
    for r in rows:
        print("  ".join(str(c).ljust(widths[i]) for i, c in enumerate(r)))


def _fail(msg: str, code: int = 1) -> None:
    print(f"error: {msg}", file=sys.stderr)
    raise SystemExit(code)


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    rc = args.func(args)
    return rc if isinstance(rc, int) else 0


if __name__ == "__main__":
    raise SystemExit(main())
