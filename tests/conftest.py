"""Shared test helpers: synthetic folded payloads, geo fixtures (all offline)."""

from __future__ import annotations

import io

from esimdb_checker import config
from esimdb_checker.geo import Geo
from esimdb_checker.models import Country


class FakeStream(io.StringIO):
    """A StringIO that reports itself as (or as not) a TTY, for CLI dispatch tests."""

    def __init__(self, text: str = "", tty: bool = False):
        super().__init__(text)
        self._tty = tty

    def isatty(self) -> bool:  # noqa: D102
        return self._tty


def feed(answers):
    """Turn a list into an `ask` callable that raises EOFError when exhausted (like stdin)."""
    it = iter(answers)

    def ask():
        try:
            return next(it)
        except StopIteration:
            raise EOFError from None
    return ask


def make_country(code: str, name: str, slug: str, region: str = "europe", popularity: int = 100) -> dict:
    return {
        "code": code,
        "name": name,
        "slug": slug,
        "region": region,
        "subregions": [],
        "popularity": popularity,
    }


DEFAULT_COUNTRIES = [
    make_country("ES", "Spain", "spain"),
    make_country("FR", "France", "france"),
    make_country("IT", "Italy", "italy"),
    make_country("JP", "Japan", "japan", region="asia"),
    make_country("GB", "United Kingdom", "united-kingdom"),
]

DEFAULT_REGIONS = [
    {
        "slug": "europe",
        "name": "Europe",
        "countries": [{"code": "ES"}, {"code": "FR"}, {"code": "IT"}],
        "searches": ["europe", "eu"],
    },
    {
        "slug": "asia",
        "name": "Asia",
        "countries": [{"code": "JP"}],
        "searches": ["asia"],
    },
]


def make_provider(pid: str, name: str, slug: str, popularity: int = 100) -> dict:
    return {"id": pid, "name": name, "slug": slug, "popularity": popularity, "logoUrl": ""}


def make_geo() -> Geo:
    """Build a Geo directly (no HTTP) for coverage/filter tests."""
    countries = [
        Country(code=c["code"], name=c["name"], slug=c["slug"],
                region=c["region"], subregions=c["subregions"], popularity=c["popularity"])
        for c in DEFAULT_COUNTRIES
    ]
    region_codes = {r["slug"]: {c["code"] for c in r["countries"]} for r in DEFAULT_REGIONS}
    region_alias = {}
    for r in DEFAULT_REGIONS:
        region_alias[r["slug"]] = r["slug"]
        region_alias[r["name"].lower()] = r["slug"]
        for a in r.get("searches", []):
            region_alias[str(a).lower()] = r["slug"]
    return Geo(countries=countries, region_codes=region_codes, region_alias=region_alias)


def build_plan_entry(
    *,
    plan_id: str,
    provider: dict,
    name: str,
    capacity_mb: int,
    period_days: int,
    prices: dict[str, float],
    data_cap_per: str | None = None,
    reduced_speed: float | None = None,
    speed_limit: float | None = None,
    possible_throttling: bool = False,
    notes: str | None = None,
    has5g: bool = False,
) -> dict:
    fam = {
        "provider": provider["id"],
        "speedLimit": speed_limit,
        "reducedSpeed": reduced_speed,
        "possibleThrottling": possible_throttling,
        "tethering": None,
        "has5G": has5g,
        "isLowLatency": False,
        "subscription": False,
        "subscription_period": None,
        "networks": [],
        "hasAds": False,
        "payAsYouGo": False,
        "newUserOnly": False,
    }
    row = [None, plan_id, capacity_mb, period_days, prices, None, name]
    if data_cap_per is not None:
        row.append(data_cap_per)
    else:
        row.append(None)
    if notes is not None:
        row.append(notes)
    return {"family": fam, "row": row, "provider": provider}


def link_rows(entries: list[dict]) -> tuple[list, list]:
    """Assign family indices into each row; return (families, rows)."""
    families = [e["family"] for e in entries]
    rows = []
    for i, e in enumerate(entries):
        row = list(e["row"])
        row[0] = i
        rows.append(row)
    return families, rows


def build_plans_payload(entries: list[dict]) -> dict:
    families, rows = link_rows(entries)
    providers = {e["provider"]["id"]: e["provider"] for e in entries}
    return {"plans": {"rows": rows, "families": families}, "providers": providers}


def mock_geo_routes(router, countries=DEFAULT_COUNTRIES, regions=DEFAULT_REGIONS):
    router.get(config.countries_endpoint()).respond(200, json=countries)
    router.get(config.regions_endpoint()).respond(200, json=regions)


def mock_plans_route(router, slug: str, payload: dict):
    router.get(config.plans_endpoint(slug)).respond(200, json=payload)


def mock_fx_route(router, currency: str = "EUR", rate: float = 0.92):
    """Mock the Frankfurter FX endpoint for `currency` (rate = 1 USD -> rate units)."""
    router.get(config.fx_endpoint(currency)).respond(
        200,
        json={"amount": 1.0, "base": "USD", "date": "2024-06-01", "rates": {currency: rate}},
    )
