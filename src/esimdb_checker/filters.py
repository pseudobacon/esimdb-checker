"""Speed-cap detection (required strict filter) + basic purchasability sanity.

Requirement: plans with speed caps MUST be filtered out, and the reason must be
recorded in the report. We detect speed caps from three independent signals so
that a plan hidden by the site's own "Hide plans with speed caps" toggle is also
caught here:

  1. structured field  `speedLimit` (Mbps)          -> explicit cap
  2. structured field  `reducedSpeed` (kbps)        -> throttled after cap
  3. structured flag   `possibleThrottling`         -> site "possible slowdowns"
  4. plan name / notes  throttling text (strong)    -> e.g. "384kbps after cap",
                                                       "then 50kbps", "throttled"
"""

from __future__ import annotations

import html
import re

from .models import Plan

# Strong throttling text patterns (applied to lowercased name + notes).
_TEXT_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("throttling word", re.compile(r"\bthrottl\w*|throttl\w*|degrad\w*|slowdown\w*\b", re.I)),
    ("speed unit after cap", re.compile(r"\b\d+\s*(kbps|kb/s|kbps/s|mbps|mb/s)\b", re.I)),
    ("kbps/kb-s token", re.compile(r"\b(kbps|kb/s|kbps)\b", re.I)),
    ("explicit 'after cap' phrasing", re.compile(r"\bafter\s+(the\s+)?cap\b", re.I)),
    ("then <speed> phrasing", re.compile(r"\bthen\s+\d+\s*(kbps|mbps|kb/s|mb/s)\b", re.I)),
    ("reduced/slower phrasing", re.compile(r"\b(reduced|slower|slowed|slowness)\b", re.I)),
]


def _fmt_speed_mbps(v: float) -> str:
    return f"{int(v)} Mbps" if float(v).is_integer() else f"{v} Mbps"


def _fmt_speed_kbps(v: float) -> str:
    if v >= 1000:
        mbps = v / 1000
        return f"{int(mbps)} Mbps" if float(mbps).is_integer() else f"{mbps} Mbps"
    return f"{int(v)} kbps"


def speed_cap_reasons(plan: Plan) -> list[str]:
    reasons: list[str] = []
    if plan.speed_limit is not None:
        reasons.append(f"explicit speed limit: {_fmt_speed_kbps(plan.speed_limit)}")
    if plan.reduced_speed is not None:
        reasons.append(f"speed reduced to {_fmt_speed_kbps(plan.reduced_speed)} after cap")
    if plan.possible_throttling:
        reasons.append("site 'possible slowdowns' flag (possibleThrottling)")

    text = html.unescape(plan.name) + " " + (plan.notes or "")
    low = text.lower()
    for label, pattern in _TEXT_PATTERNS:
        m = pattern.search(text)
        if m:
            snippet = m.group(0).strip()
            reasons.append(f"throttling text: '{snippet}' ({label})")
    # de-duplicate, preserve order
    seen: set[str] = set()
    out: list[str] = []
    for r in reasons:
        if r not in seen:
            seen.add(r)
            out.append(r)
    return out


def has_speed_cap(plan: Plan) -> bool:
    return bool(speed_cap_reasons(plan))


def purchasable(plan: Plan) -> tuple[bool, list[str]]:
    """Basic sanity for a real purchase. Returns (ok, caveats).

    A plan is purchasable if it lists at least one positive price (in any
    currency). USD is not required: ranking falls back to FX for non-USD plans.
    """
    caveats: list[str] = []
    if not plan.prices:
        return False, ["no price listed (not purchasable)"]
    has_positive = any((v is not None and v > 0) for v in plan.prices.values())
    if not has_positive:
        caveats.append("no positive price found")
    if plan.new_user_only:
        caveats.append("new-user-only offer (may not apply)")
    if plan.pay_as_you_go:
        caveats.append("pay-as-you-go plan (not a fixed allotment)")
    if plan.has_ads:
        caveats.append("may include ads (hasAds)")
    if not has_positive:
        return False, caveats
    return True, caveats


def meets_data_need_for_days(plan: Plan, data_gb: float, days: int) -> bool:
    need_mb = data_gb * 1000.0
    if plan.unlimited:
        return True
    total = plan.effective_total_mb(days)
    return (total or 0) >= need_mb
