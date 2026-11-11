"""Resolve a plan's coverage scope from its name (labeling + cross-check).

Important: this is *not* the authoritative coverage gate. esimdb.com's
destination pages only list plans that are usable in that destination, so the
recommendation engine verifies coverage by a plan's membership on every
itinerary country's page (see recommend.py). The name-based scope here produces
a human-readable label and flags plans whose scope is ambiguous ("partial" or
"unverified"), which the task requires us to surface.

Rules
-----
* Global markers ("global", "world", "worldwide", "all countries") -> all codes.
* Region markers (from the regions endpoint's names/aliases) -> that region's
  country codes (union when several regions appear, e.g. "Asia+Europe").
* Country names (and common aliases like UK/USA) -> those country codes.
* "no/except/without X" -> subtract X from the resolved scope.
* Ambiguous scopes ("3 countries", "Europe Lite", a region with a count smaller
  than the canonical region) -> "partial". Nothing resolvable -> "unverified".
"""

from __future__ import annotations

import html
import re
from collections.abc import Iterable

from .geo import Geo
from .models import Coverage


def _flex(token: str) -> str:
    """Regex for `token` where spaces/hyphens match either separator."""
    out: list[str] = []
    i = 0
    while i < len(token):
        ch = token[i]
        if ch in " -":
            j = i
            while j < len(token) and token[j] in " -":
                j += 1
            out.append(r"[ \-]+")
            i = j
        else:
            out.append(re.escape(ch))
            i += 1
    return "".join(out)


def _word_re(tokens: Iterable[str]) -> re.Pattern:
    """One alternation regex, longest token first, with word boundaries."""
    toks = sorted((t for t in tokens if t), key=len, reverse=True)
    if not toks:
        return re.compile("(?!a)a")  # never matches
    return re.compile(r"(?<![a-z0-9])(" + "|".join(_flex(t.lower()) for t in toks) + r")(?![a-z0-9])")


_GLOBAL_RE = re.compile(r"\b(global|world|worldwide|all countries|everywhere|global e?sim)\b", re.I)
_EXCEPT_RE = re.compile(r"\b(?:no|except|excluding|without|excl\.?)\b", re.I)
_COUNT_RE = re.compile(r"(\d+)\s*\+?\s*(countries?|areas?|regions?|destinations?)\b", re.I)
_LITE_RE = re.compile(r"\b(lite|lite e?sim|basic)\b", re.I)


class CoverageResolver:
    def __init__(self, geo: Geo) -> None:
        self.geo = geo
        # Country tokens for substring scanning: full names + curated aliases.
        # (We deliberately do NOT scan bare 2-letter codes — they collide.)
        country_tokens = set()
        for c in geo.countries:
            if c.name:
                country_tokens.add(c.name.lower())
        # Rebuild the alias -> code map locally (full names already cover most).
        self._country_map: dict[str, str] = {c.name.lower(): c.code for c in geo.countries if c.name}
        self._country_re = _word_re(self._country_map)
        # Region tokens (exclude the global pseudo-region, handled separately).
        region_tokens = {}
        for alias, slug in geo.region_alias.items():
            if slug == "global":
                continue
            region_tokens[alias] = slug
        self._region_map: dict[str, str] = region_tokens
        self._region_re = _word_re(region_tokens)

    def _countries_in(self, low: str) -> set[str]:
        codes = set()
        for m in self._country_re.finditer(low):
            name = m.group(1)
            code = self._country_map.get(name)
            if code:
                codes.add(code)
        return codes

    def _regions_in(self, low: str) -> set[str]:
        slugs = set()
        for m in self._region_re.finditer(low):
            slug = self._region_map.get(m.group(1))
            if slug:
                slugs.add(slug)
        return slugs

    def _exceptions_in(self, low: str) -> set[str]:
        exc: set[str] = set()
        for m in _EXCEPT_RE.finditer(low):
            seg = low[m.end(): m.end() + 45]
            exc |= self._countries_in(seg)
        return exc

    def resolve(self, name: str) -> Coverage:
        text = html.unescape(name or "")
        low = text.lower()
        has_lite = bool(_LITE_RE.search(low))

        codes: set[str] = set()
        is_global = bool(_GLOBAL_RE.search(low))
        regions = self._regions_in(low)
        countries = self._countries_in(low)
        exceptions = self._exceptions_in(low)

        matched_regions = [s for s in regions]
        if is_global:
            codes = set(self.geo.all_codes)
            base_label = "Global"
        elif matched_regions:
            for s in matched_regions:
                codes |= self.geo.region_codes_for(s)
            base_label = " + ".join(_pretty(s) for s in sorted(matched_regions))
        elif countries:
            codes = set(countries)
            base_label = self._country_names(codes)
        else:
            return Coverage(
                status="unverified",
                label=text.strip() or "unspecified",
                codes=[],
                note="coverage scope could not be resolved from the plan name",
            )

        removed = codes & exceptions
        codes -= removed

        count = _COUNT_RE.search(low)
        subset = False
        if removed:
            subset = True
        if has_lite:
            subset = True
        if count and len(matched_regions) == 1:
            region_size = len(self.geo.region_codes_for(matched_regions[0]))
            n = int(count.group(1))
            if region_size and n < region_size:
                subset = True

        label = base_label
        if removed:
            label += " " + self._country_names(removed, prefix="minus ")
        if has_lite:
            label += " (subset)"

        status = "partial" if subset else "verified"
        note = ""
        if subset:
            note = "name indicates a subset/exclusion; site listing is authoritative"
        elif not codes:
            status = "partial"
            note = "explicit exclusions leave an empty resolved set"
        return Coverage(status=status, label=label.strip(), codes=sorted(codes), note=note)

    def _country_names(self, codes: set[str], prefix: str = "") -> str:
        names = []
        by_code = {c.code: c.name for c in self.geo.countries}
        for code in sorted(codes):
            names.append(by_code.get(code, code))
        if not names:
            return ""
        return prefix + ", ".join(names)


def _pretty(slug: str) -> str:
    return slug.replace("-", " ").title()


def resolve_coverage(name: str, geo: Geo) -> Coverage:
    return CoverageResolver(geo).resolve(name)
