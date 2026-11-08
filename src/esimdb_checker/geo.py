"""Geography: destinations, regions, and canonical membership.

Loaded from the observed client endpoints (/api/client/countries and
/api/client/regions) and cached as snapshots. Provides country resolution
(name/code/slug -> canonical Country) and region -> country-code sets used by
the coverage resolver.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable, Optional

import httpx

from . import config, http
from .models import Country


# Common aliases for countries whose display name differs from the token users
# are likely to type. Merged on top of the endpoint-provided names/slugs.
_COUNTRY_ALIASES: dict[str, str] = {
    "uk": "GB",
    "united kingdom": "GB",
    "usa": "US",
    "united states": "US",
    "united states of america": "US",
    "u.s.": "US",
    "u.s.a": "US",
    "ukraine": "UA",
    "russia": "RU",
    "czechia": "CZ",
    "czech republic": "CZ",
    "laos": "LA",
    "switzerland": "CH",
    "great britain": "GB",
}


class Geo:
    def __init__(
        self,
        countries: list[Country],
        region_codes: dict[str, set[str]],
        region_alias: dict[str, str],
    ) -> None:
        self.countries = countries
        self.region_codes = region_codes
        self.region_alias = region_alias
        self._by_code = {c.code: c for c in countries}
        self._by_slug = {c.slug: c for c in countries}
        self._by_name = {c.name.lower(): c for c in countries}
        self._by_alias = {}
        for token, code in _COUNTRY_ALIASES.items():
            if code in self._by_code:
                self._by_alias[token] = self._by_code[code]
        # Region slugs/names also resolve to a pseudo-lookup used by coverage.
        self.all_codes: set[str] = set(self._by_code)

    # ---- loading ----------------------------------------------------------
    @classmethod
    def load(cls, client: httpx.Client, data_dir: Path, use_cache: bool = True) -> "Geo":
        countries_raw, c_meta = http.fetch_json(
            client, config.countries_endpoint(), data_dir, use_cache
        )
        regions_raw, r_meta = http.fetch_json(
            client, config.regions_endpoint(), data_dir, use_cache
        )

        countries: list[Country] = []
        for c in countries_raw:
            countries.append(
                Country(
                    code=c.get("code") or c.get("_id"),
                    name=c.get("name", ""),
                    slug=c.get("slug", ""),
                    region=c.get("region"),
                    subregions=tuple(c.get("subregions") or []),
                    popularity=int(c.get("popularity") or 0),
                )
            )

        region_codes: dict[str, set[str]] = {}
        region_alias: dict[str, str] = {}
        for r in regions_raw:
            slug = r.get("slug", "")
            region_codes[slug] = {c["code"] for c in r.get("countries", [])}
            region_alias[slug] = slug
            if r.get("name"):
                region_alias[r["name"].lower()] = slug
            for a in r.get("searches", []):
                region_alias[str(a).lower()] = slug
        g = cls(countries, region_codes, region_alias)
        g._last_manifests = {"countries": c_meta, "regions": r_meta}
        return g

    @property
    def manifests(self) -> dict:
        return getattr(self, "_last_manifests", {})

    # ---- resolution -------------------------------------------------------
    def resolve_country(self, token: str) -> Optional[Country]:
        t = token.strip()
        if not t:
            return None
        if t.upper() in self._by_code:
            return self._by_code[t.upper()]
        tl = t.lower()
        if tl in self._by_slug:
            return self._by_slug[tl]
        if tl in self._by_name:
            return self._by_name[tl]
        if tl in self._by_alias:
            return self._by_alias[tl]
        return None

    def candidates(self, token: str) -> list[Country]:
        """Countries whose name/code/slug (or a known alias) contains the token,
        for disambiguation in the wizard. Stable catalog order."""
        t = token.strip().lower()
        if not t:
            return []
        out: list[Country] = []
        seen: set[str] = set()
        for c in self.countries:
            if t in c.name.lower() or t in c.code.lower() or t in c.slug:
                out.append(c)
                seen.add(c.code)
        for alias, c in self._by_alias.items():
            if t in alias and c.code not in seen:
                out.append(c)
                seen.add(c.code)
        return out

    def region_codes_for(self, slug: str) -> set[str]:
        return self.region_codes.get(slug, set())

    def region_for_alias(self, alias: str) -> Optional[str]:
        return self.region_alias.get(alias.lower())

    def all_country_tokens(self) -> set[str]:
        """Lowercase tokens that name a country (for the coverage scanner)."""
        toks = set()
        for c in self.countries:
            toks.add(c.name.lower())
            toks.add(c.code.lower())
        toks.update(self._by_alias)
        # multi-word names are handled via substring scan in coverage.py; keep
        # single-token forms here.
        return toks


def resolve_many(geo: Geo, tokens: Iterable[str]) -> tuple[list[Country], list[str]]:
    """Resolve many tokens; return (countries, unresolved_tokens)."""
    found: list[Country] = []
    seen: set[str] = set()
    unresolved: list[str] = []
    for tok in tokens:
        c = geo.resolve_country(tok)
        if c is None:
            unresolved.append(tok)
        elif c.code not in seen:
            seen.add(c.code)
            found.append(c)
    return found, unresolved
