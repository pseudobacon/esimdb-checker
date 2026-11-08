"""Typed data model.

The core unit of an itinerary is a *leg*: one resolved destination with a
positive integer duration (in sequential travel-day slots) and a derived data
requirement. A *Trip* is an ordered tuple of legs plus a daily data target, a
display/fees currency, and an optional total budget. Trip totals are derived
from the legs (never flattened away): total_days = sum(leg.days), required_gb =
total_days * daily_data_gb.

A *Strategy* is a complete, verifiable set of purchases that satisfies every
leg: each leg's data need is met by plan(s) that cover it, are valid for the
leg's travel days, and (for shared regional plans) draw from one shared pool.
Strategies come in three families: local-only, whole-trip (one plan covering
all legs), and hybrid (one regional plan serving a subset of whole legs plus
local plans for the rest).

Money is carried as Decimal where it enters from the API and converted to
float only at the display boundary, so totals are exact and rounding happens
once, on purpose.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Optional

# ---------------------------------------------------------------------------
# country
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Country:
    code: str
    name: str
    slug: str
    region: Optional[str] = None
    subregions: tuple = ()
    popularity: int = 0
    aliases: tuple[str, ...] = ()

    @property
    def key(self) -> str:
        return self.code.lower()


# ---------------------------------------------------------------------------
# plan
# ---------------------------------------------------------------------------
@dataclass
class Plan:
    id: str
    name: str
    provider: str
    provider_slug: Optional[str]
    prices: dict[str, float]          # currency code -> price (USD always present)
    capacity_mb: int                   # 0 = unlimited pool; per-day amount when data_cap_per == "day"
    period_days: int                   # 0 = unknown validity
    data_cap_per: Optional[str] = None # "day" (daily-refill) | None
    speed_limit: Optional[float] = None   # kbps (None = not speed-capped)
    reduced_speed: Optional[float] = None  # kbps
    possible_throttling: Optional[bool] = None
    activation_type: Optional[str] = None
    source_slug: Optional[str] = None
    provider_popularity: int = 0
    # Extra family attributes from the folded API (retained for provenance).
    tethering: Optional[bool] = None
    has_5g: bool = False
    is_low_latency: bool = False
    subscription: bool = False
    subscription_period: Optional[str] = None
    networks: list = field(default_factory=list)
    has_ads: bool = False
    pay_as_you_go: bool = False
    new_user_only: bool = False
    notes: Optional[str] = None

    # ---- derived values ---------------------------------------------------
    @property
    def unlimited(self) -> bool:
        return self.capacity_mb == 0

    @property
    def usd_price(self) -> Optional[float]:
        v = self.prices.get("USD")
        return float(v) if v is not None else None

    @property
    def data_gb(self) -> float:
        """Total pool data in GB (0 if unlimited/unknown)."""
        if self.unlimited:
            return 0.0
        return self.capacity_mb / 1024.0

    @property
    def daily_cap_gb(self) -> Optional[float]:
        """Per-day data cap in GB, for daily-refill plans (capacity_mb is the per-day amount)."""
        if self.is_daily_refill():
            return self.capacity_mb / 1024.0
        return None

    def is_daily_refill(self) -> bool:
        return self.data_cap_per == "day"

    def effective_total_mb(self, days: int) -> Optional[int]:
        """Total data deliverable over `days` (None when unlimited).

        Daily-refill plans deliver capacity_mb per day (so capacity_mb * days
        over the period); pool plans deliver capacity_mb once for the validity.
        """
        if self.unlimited:
            return None
        if self.is_daily_refill():
            return self.capacity_mb * max(days, 1)
        return self.capacity_mb

    def price_in(self, currency: str, fx: Optional["Fx"] = None) -> tuple[Optional[float], bool]:
        """Price in the requested currency, and whether it is price-comparable.

        Prefers a native price in the requested currency; otherwise converts the
        always-present USD price via the disclosed FX rate. Returns (None, False)
        only when conversion is impossible (target not USD, no native price, and
        FX unavailable).
        """
        currency = currency.upper()
        if currency in self.prices:
            return float(self.prices[currency]), True
        if currency == "USD":
            return self.usd_price, self.usd_price is not None
        if self.usd_price is not None and fx is not None and fx.available and fx.currency == currency and fx.rate is not None:
            return self.usd_price * fx.rate, True
        return None, False

    def price_per_gb(self, currency: str, fx: Optional["Fx"] = None) -> Optional[float]:
        price, ok = self.price_in(currency, fx)
        if not ok or price is None or self.unlimited or self.data_gb <= 0:
            return None
        return price / self.data_gb

    def capacity_label(self) -> str:
        if self.unlimited:
            return "Unlimited"
        mb = self.capacity_mb
        per = "/day" if self.is_daily_refill() else ""
        if mb >= 1024:
            return f"{mb / 1024:g} GB{per}"
        return f"{mb} MB{per}"

    def period_label(self) -> str:
        if self.period_days == 0:
            return "—"
        return f"{self.period_days} days"

    def data_cap_label(self) -> str:
        if not self.is_daily_refill():
            return ""
        gb = self.capacity_mb / 1024
        return f"up to {gb:g} GB/day"

    def speed_label(self) -> str:
        parts: list[str] = []
        if self.speed_limit:
            parts.append(f"{self.speed_limit / 1000:g} Mbps cap")
        if self.reduced_speed:
            parts.append(str(self.reduced_speed))
        if self.possible_throttling:
            parts.append("possible throttling")
        return " · ".join(parts) if parts else ""


# ---------------------------------------------------------------------------
# FX (disclosed conversion source)
# ---------------------------------------------------------------------------
@dataclass
class Fx:
    base: str                      # "USD"
    currency: str                  # requested/target currency
    rate: Optional[float]          # 1 USD -> rate units of `currency`
    source: str
    date: str
    available: bool
    note: str = ""

    def convert_usd(self, usd: Optional[float]) -> Optional[float]:
        if usd is None or not self.available or self.rate is None:
            return None
        return usd * self.rate

    def to_dict(self) -> dict[str, Any]:
        return {
            "base": self.base,
            "currency": self.currency,
            "rate": self.rate,
            "source": self.source,
            "date": self.date,
            "available": self.available,
            "note": self.note,
        }


# ---------------------------------------------------------------------------
# itinerary: legs + trip
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Leg:
    country: Optional[Country]   # None for a pure transfer leg (no destination)
    days: int          # charged destination days (>= 0; 0 = transfer leg)
    start_day: int     # 1-based travel-day index, inclusive (start > end for 0-day legs)
    end_day: int       # inclusive
    required_gb: float  # days * daily_data_gb (0 for transfer legs)
    transfer_days: int = 0  # informational uncharged transit/connectivity days

    @property
    def is_transfer(self) -> bool:
        return self.days == 0 or self.country is None

    @property
    def country_name(self) -> str:
        return self.country.name if self.country else "(transfer)"

    def label(self) -> str:
        if self.is_transfer:
            s = f"transfer leg (no destination)"
            if self.transfer_days:
                s += f", {self.transfer_days} transfer day(s)"
            return s
        return (
            f"{self.country_name}: days {self.start_day}–{self.end_day}, "
            f"{self.days} day(s), {_gb(self.required_gb)} GB required"
        )

    def travel_days_label(self) -> str:
        """Human-readable travel-day range for this leg (handles 0-day legs)."""
        if self.days > 0:
            return f"{self.start_day}–{self.end_day}"
        if self.transfer_days > 0:
            a = self.start_day - self.transfer_days
            b = self.start_day - 1
            span = f"{a}–{b}" if a != b else str(a)
            return f"{span} (transfer)"
        return "—"


@dataclass(frozen=True)
class Trip:
    legs: tuple[Leg, ...]
    daily_data_gb: float
    currency: str
    budget: Optional[float] = None  # total budget in `currency`, or None (unbounded)

    @property
    def total_days(self) -> int:
        return sum(leg.days for leg in self.legs)

    @property
    def required_gb(self) -> float:
        return self.total_days * self.daily_data_gb

    @property
    def names(self) -> list[str]:
        return [leg.country.name for leg in self.legs if leg.country is not None]

    @property
    def slugs(self) -> list[str]:
        return [leg.country.slug for leg in self.legs if leg.country is not None]

    @property
    def distinct_countries(self) -> list[Country]:
        seen: dict[str, Country] = {}
        for leg in self.legs:
            if leg.country is None:
                continue
            seen.setdefault(leg.country.key, leg.country)
        return list(seen.values())

    def legs_for_slug(self, slug: str) -> list[Leg]:
        return [leg for leg in self.legs if leg.country.slug == slug]

    def to_dict(self) -> dict[str, Any]:
        return {
            "legs": [
                {
                    "country": leg.country.name,
                    "slug": leg.country.slug,
                    "code": leg.country.code,
                    "days": leg.days,
                    "start_day": leg.start_day,
                    "end_day": leg.end_day,
                    "required_gb": _gb(leg.required_gb),
                }
                for leg in self.legs
            ],
            "daily_data_gb": _gb(self.daily_data_gb),
            "currency": self.currency,
            "budget": _gb(self.budget) if self.budget is not None else None,
            "total_days": self.total_days,
            "required_gb": _gb(self.required_gb),
        }


def _gb(v: Optional[float]) -> Optional[float]:
    if v is None:
        return None
    # Avoid spurious float noise (e.g. 0.30000000000000004) for display/JSON.
    return round(float(v), 6)


# ---------------------------------------------------------------------------
# coverage (plan-name scope, for labelling only)
# ---------------------------------------------------------------------------
@dataclass
class Coverage:
    status: str  # "local" | "regional" | "multi" | "unverified" | "partial"
    codes: tuple[str, ...]
    label: str
    note: str = ""


# ---------------------------------------------------------------------------
# strategies
# ---------------------------------------------------------------------------
@dataclass
class StrategyPlan:
    plan: Plan
    legs: tuple[int, ...]      # indices of the itinerary legs this plan serves
    activation_day: int        # travel-day the plan is activated on
    cost: Optional[float]      # in the requested currency (None = not comparable)
    comparable: bool
    assumptions: list[str] = field(default_factory=list)
    url: Optional[str] = None  # provider page URL (filled by the engine)

    def to_dict(self) -> dict[str, Any]:
        return {
            "plan_id": self.plan.id,
            "plan": self.plan.name,
            "provider": self.plan.provider,
            "serves_legs": list(self.legs),
            "activation_day": self.activation_day,
            "cost": _gb(self.cost),
            "price_comparable": self.comparable,
            "assumptions": list(self.assumptions),
            "url": self.url,
        }


@dataclass
class Strategy:
    family: str                 # "local_only" | "whole_trip" | "hybrid"
    plans: tuple[StrategyPlan, ...]
    total_cost: Optional[float]  # in the requested currency (None = not comparable)
    currency: str
    comparable: bool
    meets_data: bool
    validity_ok: bool
    assumptions: list[str] = field(default_factory=list)

    @property
    def purchase_count(self) -> int:
        return len(self.plans)

    def to_dict(self) -> dict[str, Any]:
        return {
            "family": self.family,
            "total_cost": _gb(self.total_cost),
            "currency": self.currency,
            "price_comparable": self.comparable,
            "meets_data": self.meets_data,
            "validity_ok": self.validity_ok,
            "purchases": self.purchase_count,
            "assumptions": list(self.assumptions),
            "plans": [p.to_dict() for p in self.plans],
        }


# ---------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------
@dataclass
class Report:
    trip: Trip
    generated_at: str
    source_urls: list[str]
    snapshot_manifest: dict[str, dict[str, Any]]
    fx: dict[str, Any]
    recommended: Optional[Strategy]
    strategies: dict[str, Optional[Strategy]]   # local_only / whole_trip / hybrid
    budget_status: dict[str, Any]
    diagnostics: dict[str, Any]
    totals: dict[str, Any]
    speed_filter_status: dict[str, Any]
    notes: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "itinerary": self.trip.to_dict(),
            "generated_at": self.generated_at,
            "fx": self.fx,
            "recommended": self.recommended.to_dict() if self.recommended else None,
            "strategies": {
                k: (v.to_dict() if v else None) for k, v in self.strategies.items()
            },
            "budget_status": self.budget_status,
            "diagnostics": self.diagnostics,
            "totals": self.totals,
            "speed_filter_status": self.speed_filter_status,
            "source_urls": self.source_urls,
            "snapshot_manifest": self.snapshot_manifest,
            "notes": self.notes,
        }
