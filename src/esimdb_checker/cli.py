"""Command-line interface.

Subcommands:
  recommend  Fetch plans for an itinerary, verify coverage, filter, and rank.
  plans      List plans for one destination (debugging / exploration).
  countries  List/resolve destination countries.
  resolve    Show how a token resolves to a canonical country.
  fetch      Store a raw snapshot of an endpoint URL (reproducibility).
"""

from __future__ import annotations

import argparse
import difflib
import sys
from pathlib import Path

import httpx

from . import catalog, config, filters, http, report, snapshots
from .geo import Geo, resolve_many
from .models import Itinerary
from .recommend import RecommendationEngine


def _client() -> httpx.Client:
    return http.build_client()


def _close_country_suggestions(geo: Geo, token: str, k: int = 5) -> list[str]:
    names = [c.name.lower() for c in geo.countries]
    return difflib.get_close_matches(token.lower(), names, n=k, cutoff=0.5)


def _data_dir(args) -> Path:
    if getattr(args, "data_dir", None):
        p = Path(args.data_dir)
        return p if p.is_absolute() else Path.cwd() / p
    return config.data_dir()


def _print_error(msg: str) -> None:
    print(f"error: {msg}", file=sys.stderr)


# ---------------------------------------------------------------------------
# subcommands
# ---------------------------------------------------------------------------
def cmd_recommend(args) -> int:
    data_dir = _data_dir(args)
    use_cache = not args.no_cache
    with _client() as client:
        geo = Geo.load(client, data_dir, use_cache)
        found, unresolved = resolve_many(geo, args.countries)
        if unresolved:
            for tok in unresolved:
                sugg = _close_country_suggestions(geo, tok)
                hint = f" (did you mean: {', '.join(sugg)})" if sugg else ""
                _print_error(f"unrecognized country '{tok}'{hint}")
            return 3
        itinerary = Itinerary(
            countries=found,
            days=args.days,
            data_gb=args.data_gb,
            currency=args.currency.upper(),
        )
        engine = RecommendationEngine(
            client,
            data_dir,
            use_cache=use_cache,
            top_n=args.top,
            provider=args.provider,
            include_capped=args.include_capped,
        )
        engine.load(itinerary)
        report_obj = engine.recommend(itinerary)

    if args.json:
        print(report.render_json(report_obj))
    else:
        print(report.render_console(report_obj))
    if args.md:
        report.write_report(report_obj, Path(args.md), fmt="markdown")
        print(f"\n(markdown report written to {args.md})", file=sys.stderr)
    return 0


def cmd_plans(args) -> int:
    data_dir = _data_dir(args)
    use_cache = not args.no_cache
    with _client() as client:
        geo = Geo.load(client, data_dir, use_cache)
        country = geo.resolve_country(args.country)
        if country is None:
            sugg = _close_country_suggestions(geo, args.country)
            hint = f" (did you mean: {', '.join(sugg)})" if sugg else ""
            _print_error(f"unrecognized country '{args.country}'{hint}")
            return 3
        plans, _ = catalog.fetch_country_plans(client, country.slug, data_dir, use_cache)
        if args.provider:
            plans = [p for p in plans if p.provider.lower() == args.provider.lower()]
        plans.sort(key=lambda p: (-(p.capacity_mb if not p.unlimited else 10**9), p.usd_price or 0))
        print(f"{len(plans)} plans for {country.name} (/{country.slug})")
        print(f"{'PRICE':>12}  {'DATA':>12}  {'PERIOD':>9}  {'DAILY':>12}  {'SPEEDCAP':>9}  PLAN")
        for p in plans[: args.limit]:
            price = f"{p.prices.get(args.currency.upper(), p.usd_price or 0):g}"
            cap = p.capacity_label()
            period = p.period_label()
            daily = p.data_cap_label() or "-"
            sc = "YES" if filters.has_speed_cap(p) else ""
            print(f"{price:>12}  {cap:>12}  {period:>9}  {daily:>12}  {sc:>9}  {p.name}")
    return 0


def cmd_countries(args) -> int:
    data_dir = _data_dir(args)
    with _client() as client:
        geo = Geo.load(client, data_dir, not args.no_cache)
    q = (args.query or "").lower()
    region = (args.region or "").lower()
    for c in geo.countries:
        if q and q not in c.name.lower() and q not in c.code.lower() and q not in c.slug:
            continue
        if region and (c.region or "").lower() != region and region not in (c.slug or ""):
            continue
        print(f"{c.code:>4}  {c.name:<24} {c.slug:<22} {c.region or ''}")
    return 0


def cmd_resolve(args) -> int:
    data_dir = _data_dir(args)
    with _client() as client:
        geo = Geo.load(client, data_dir, not args.no_cache)
    rc = 0
    for tok in args.tokens:
        c = geo.resolve_country(tok)
        if c:
            print(f"{tok:<16} -> {c.code} {c.name} (/{c.slug}, region: {c.region or 'n/a'})")
        else:
            sugg = _close_country_suggestions(geo, tok)
            hint = f" [did you mean: {', '.join(sugg)}]" if sugg else ""
            print(f"{tok:<16} -> UNRESOLVED{hint}")
            rc = 3
    return rc


def cmd_fetch(args) -> int:
    data_dir = _data_dir(args)
    with _client() as client:
        body = http.http_get(client, args.url)
    manifest = snapshots.write_cache(data_dir, args.url, body)
    print(f"stored {manifest['url']}")
    print(f"  body: {manifest['body']}")
    print(f"  sha256: {manifest['sha256']}")
    print(f"  bytes: {manifest['bytes']}")
    return 0


# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="esimdb-checker",
        description="Research travel eSIM plans on esimdb.com and recommend a purchase.",
    )
    p.add_argument("--data-dir", default=None, help="snapshot cache dir (default ./.cache)")
    sub = p.add_subparsers(dest="command", required=True)

    pr = sub.add_parser("recommend", help="recommend a whole-itinerary eSIM")
    pr.add_argument("countries", nargs="+", help="itinerary destinations (names, codes, or slugs)")
    pr.add_argument("--days", type=int, default=7, help="trip length in days (default 7)")
    pr.add_argument("--data-gb", type=float, default=10.0, help="total data need in GB (default 10)")
    pr.add_argument("--currency", default="EUR", help="display currency (default EUR)")
    pr.add_argument("--top", type=int, default=5, help="number of recommendations (default 5)")
    pr.add_argument("--provider", default=None, help="restrict to one provider (by name)")
    pr.add_argument(
        "--include-capped",
        action="store_true",
        help="also consider speed-capped plans (default excluded); they keep a caveat and rank below uncapped",
    )
    pr.add_argument("--no-cache", action="store_true", help="refetch even if a snapshot exists")
    pr.add_argument("--json", action="store_true", help="print the report as JSON")
    pr.add_argument("--md", metavar="FILE", help="also write a markdown report to FILE")
    pr.set_defaults(func=cmd_recommend)

    pp = sub.add_parser("plans", help="list plans for one destination")
    pp.add_argument("country", help="destination (name, code, or slug)")
    pp.add_argument("--provider", default=None, help="filter by provider name")
    pp.add_argument("--currency", default="EUR", help="price currency (default EUR)")
    pp.add_argument("--limit", type=int, default=50, help="max rows (default 50)")
    pp.add_argument("--no-cache", action="store_true")
    pp.set_defaults(func=cmd_plans)

    pc = sub.add_parser("countries", help="list destination countries")
    pc.add_argument("--query", default=None, help="substring filter")
    pc.add_argument("--region", default=None, help="region filter")
    pc.add_argument("--no-cache", action="store_true")
    pc.set_defaults(func=cmd_countries)

    prs = sub.add_parser("resolve", help="resolve a token to a canonical country")
    prs.add_argument("tokens", nargs="+", help="tokens to resolve")
    prs.add_argument("--no-cache", action="store_true")
    prs.set_defaults(func=cmd_resolve)

    pf = sub.add_parser("fetch", help="store a raw snapshot of a URL")
    pf.add_argument("url", help="URL to fetch and snapshot")
    pf.set_defaults(func=cmd_fetch)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except http.FetchError as exc:
        _print_error(str(exc))
        return 4
    except KeyboardInterrupt:
        _print_error("interrupted")
        return 130
    except Exception as exc:  # noqa: BLE001
        _print_error(f"unexpected error: {exc!r}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
