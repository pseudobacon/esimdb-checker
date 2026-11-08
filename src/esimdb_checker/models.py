"""Dataclasses shared across the package, with dict serialization for reports."""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any, Optional


def _num(x: Any, nd: int = 2) -> Optional[float]:
    """Round a number, drop trailing .0 for integers, keep None as None."""
    if x is None:
        return None
    r = round(float(x), nd)
    return int(r) if r == int(r) else r


@dataclass
class Country:
    code: str
    name: str
    slug: str
    region: Optional[str] = None
    subregions: list[str] = field(default_factory=list)
    popularity: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Plan:
    """One eSIM plan as returned by the observed folded plans API."""

    id: str
    name: str
    provider: str
    provider_slug: str
    capacity_mb: int  # 0 => unlimited data
    period_days: int  # 0 => unknown / no fixed validity
    provider_popularity: int = 0
    prices: dict[str, float] = field(default_factory=dict)
    data_cap_per: Optional[str] = None  # e.g. "day"
    speed_limit: Optional[float] = None  # kbps; hard max speed when set
    reduced_speed: Optional[float] = None  # kbps; speed after the data cap
    possible_throttling: bool = False
    tethering: Optional[bool] = None
    has_5g: bool = False
    is_low_latency: bool = False
    subscription: bool = False
    subscription_period: Optional[str] = None
    networks: list[str] = field(default_factory=list)
    has_ads: bool = False
    pay_as_you_go: bool = False
    new_user_only: bool = False
    notes: Optional[str] = None
    source_slug: str = ""  # destination page(s) the plan was listed on

    # ---- derived helpers -------------------------------------------------
    # Fallback FX used ONLY to rank plans that lack a USD price (the site's
    # primary currency). Display always uses the plan's actual currency.
    _FX_TO_USD = {"EUR": 1.08, "GBP": 1.27, "CHF": 1.11, "JPY": 0.0067}

    @property
    def usd_price(self) -> Optional[float]:
        """The plan's USD price, or None if the API does not provide one."""
        return self.prices.get("USD")

    def usd_ranking_price(self) -> Optional[float]:
        """A USD-comparable value for ranking: USD if present, else a fallback
        FX conversion of another present currency, else the first price."""
        if self.prices.get("USD") is not None:
            return self.prices["USD"]
        for cur, rate in self._FX_TO_USD.items():
            if self.prices.get(cur) is not None:
                return self.prices[cur] * rate
        for v in self.prices.values():
            if v is not None:
                return v
        return None

    def price_and_currency(self, requested: str) -> tuple[Optional[float], Optional[str]]:
        """(price, currency) to display: requested currency if available, else
        USD, else the plan's first available currency."""
        if requested in self.prices:
            return self.prices[requested], requested
        if "USD" in self.prices:
            return self.prices["USD"], "USD"
        for cur, v in self.prices.items():
            return v, cur
        return None, None

    @property
    def unlimited(self) -> bool:
        return self.capacity_mb == 0

    def capacity_label(self) -> str:
        if self.unlimited:
            return "Unlimited"
        mb = self.capacity_mb
        if mb >= 1000:
            gb = mb / 1000
            return f"{_num(gb)} GB"
        return f"{mb} MB"

    def period_label(self) -> str:
        if self.period_days in (0, None):
            return "—"
        return f"{self.period_days} day" + ("s" if self.period_days != 1 else "")

    def data_cap_label(self) -> str:
        if not self.data_cap_per:
            return ""
        unit = "day" if self.data_cap_per == "day" else self.data_cap_per
        # For daily-cap plans the capacity field is the per-day allowance.
        per = "Unlimited" if self.unlimited else self.capacity_label()
        return f"{per} per {unit}"

    def price_per_gb_usd(self) -> Optional[float]:
        """USD per GB for finite plans using the real USD price (None if the
        plan has no USD price or is unlimited)."""
        price = self.prices.get("USD")
        if not price or self.unlimited:
            return None
        gb = self.capacity_mb / 1000.0
        if gb <= 0:
            return None
        return round(price / gb, 2)

    def effective_total_mb(self, days: int) -> Optional[int]:
        """Rough total data the plan can deliver over a `days`-long trip.

        Daily-cap plans deliver `capacity_mb` each day for up to the validity
        window (or the trip length, whichever is shorter). Finite plans deliver
        their fixed capacity. Unlimited => None (treated as infinite).
        """
        if self.unlimited:
            return None
        if self.data_cap_per == "day":
            window = self.period_days if self.period_days and self.period_days > 0 else days
            window = max(1, min(window, max(days, 1)))
            return self.capacity_mb * window
        return self.capacity_mb

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["usd_price"] = _num(self.usd_price)
        d["price_per_gb_usd"] = _num(self.price_per_gb_usd())
        d["capacity_label"] = self.capacity_label()
        d["period_label"] = self.period_label()
        d["data_cap_label"] = self.data_cap_label()
        return d


@dataclass
class Coverage:
    """Coverage resolved from a plan name (labeling + cross-check only).

    The authoritative coverage gate is the *destination-page membership*
    (see recommend.py): a plan is verified to cover an itinerary country if it
    is listed on that country's page. `Coverage` here parses the plan name to
    produce a human-readable scope label and to flag ambiguous scopes.
    """

    status: str  # "verified" | "partial" | "unverified"
    label: str
    codes: list[str] = field(default_factory=list)
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Itinerary:
    countries: list[Country]
    days: int = 7
    data_gb: float = 10.0
    currency: str = "EUR"

    @property
    def codes(self) -> list[str]:
        return [c.code for c in self.countries]

    @property
    def slugs(self) -> list[str]:
        return [c.slug for c in self.countries]

    @property
    def names(self) -> list[str]:
        return [c.name for c in self.countries]

    def to_dict(self) -> dict[str, Any]:
        return {
            "countries": [c.to_dict() for c in self.countries],
            "days": self.days,
            "data_gb": self.data_gb,
            "currency": self.currency,
        }


@dataclass
class Recommendation:
    rank: int
    plan: Plan
    coverage: Coverage
    usd_price: Optional[float]
    display_price: Optional[float]
    currency: str
    price_per_gb: Optional[float]
    meets_data_need: bool
    reasons: list[str] = field(default_factory=list)
    caveats: list[str] = field(default_factory=list)
    url: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "rank": self.rank,
            "usd_price": _num(self.usd_price),
            "display_price": _num(self.display_price),
            "currency": self.currency,
            "price_per_gb_usd": _num(self.price_per_gb),
            "meets_data_need": self.meets_data_need,
            "reasons": self.reasons,
            "caveats": self.caveats,
            "url": self.url,
            "plan": self.plan.to_dict(),
            "coverage": self.coverage.to_dict(),
        }


@dataclass
class Report:
    itinerary: Itinerary
    generated_at: str
    source_urls: list[str]
    snapshot_manifest: dict[str, str]
    totals: dict[str, int]
    winners: list[Recommendation]
    excluded_speed_cap: list[dict[str, Any]]
    unverified_coverage: list[dict[str, Any]]
    fallback: list[dict[str, Any]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "itinerary": self.itinerary.to_dict(),
            "generated_at": self.generated_at,
            "source_urls": self.source_urls,
            "snapshot_manifest": self.snapshot_manifest,
            "totals": self.totals,
            "winners": [w.to_dict() for w in self.winners],
            "excluded_speed_cap": self.excluded_speed_cap,
            "unverified_coverage": self.unverified_coverage,
            "fallback": self.fallback,
            "notes": self.notes,
        }
