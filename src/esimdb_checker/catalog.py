"""Parse the observed 'folded' plans API into Plan objects.

The folded payload is compact/positional:

    payload = {
      "plans": {
        "families": [ {provider, speedLimit, reducedSpeed, possibleThrottling,
                       tethering, has5G, isLowLatency, subscription,
                       subscription_period, telephony, networks, hasAds,
                       payAsYouGo, newUserOnly, ...} , ... ],
        "rows": [ [familyIdx, planId, capacityMB, periodDays, prices{}, null,
                   name, dataCapPer?, notes?], ... ],
      },
      "providers": { providerId: {name, slug, popularity, ...}, ... },
    }

Rows reference a shared family by index (position 0). Prices is a per-currency
dict. `capacityMB == 0` means unlimited. `dataCapPer` (position 7) is a string
like "day" when a per-day cap applies.
"""

from __future__ import annotations

import html
from pathlib import Path
from typing import Any

import httpx

from . import config, http
from .models import Plan


def _as_int(x: Any, default: int = 0) -> int:
    try:
        return int(x)
    except (TypeError, ValueError):
        return default


def _prices(row: list[Any]) -> dict[str, float]:
    p = row[4] if len(row) > 4 and isinstance(row[4], dict) else {}
    out: dict[str, float] = {}
    for cur, val in p.items():
        try:
            out[cur] = float(val)
        except (TypeError, ValueError):
            continue
    return out


def _data_cap(row: list[Any]) -> str | None:
    if len(row) > 7 and isinstance(row[7], str):
        v = row[7].strip().lower()
        if v in {"day", "month", "week"}:
            return v
    return None


def _notes(row: list[Any]) -> str | None:
    if len(row) > 8 and isinstance(row[8], str) and row[8].strip():
        return row[8].strip()
    return None


def parse_folded(payload: dict[str, Any], source_slug: str) -> list[Plan]:
    plans_block = payload.get("plans", {})
    if not isinstance(plans_block, dict):
        return []
    families = plans_block.get("families", []) or []
    rows = plans_block.get("rows", []) or []

    providers: dict[str, dict[str, Any]] = {}
    raw_prov = payload.get("providers", {})
    if isinstance(raw_prov, dict):
        providers = {k: v for k, v in raw_prov.items() if isinstance(v, dict)}
    elif isinstance(raw_prov, list):
        providers = {p.get("_id"): p for p in raw_prov if isinstance(p, dict)}

    result: list[Plan] = []
    for row in rows:
        if not isinstance(row, list) or len(row) < 7:
            continue
        fam_idx = _as_int(row[0], -1)
        fam = families[fam_idx] if 0 <= fam_idx < len(families) and isinstance(families[fam_idx], dict) else {}
        plan_id = row[1]
        # The payload carries HTML-escaped text ("Asia &amp; Europe 52 Countries");
        # unescape once here so every renderer, matcher and report sees clean text.
        name = html.unescape(str(row[6]).strip())
        if not plan_id or not name:
            continue

        prov_id = fam.get("provider")
        prov = providers.get(prov_id, {}) if isinstance(prov_id, str) else {}

        result.append(
            Plan(
                id=str(plan_id),
                name=name,
                provider=html.unescape(str(prov.get("name") or "Unknown")),
                provider_slug=str(prov.get("slug") or ""),
                provider_popularity=_as_int(prov.get("popularity"), 0),
                capacity_mb=_as_int(row[2], 0),
                period_days=_as_int(row[3], 0),
                prices=_prices(row),
                data_cap_per=_data_cap(row),
                speed_limit=_optional_num(fam.get("speedLimit")),
                reduced_speed=_optional_num(fam.get("reducedSpeed")),
                possible_throttling=bool(fam.get("possibleThrottling")),
                tethering=_optional_bool(fam.get("tethering")),
                has_5g=bool(fam.get("has5G")),
                is_low_latency=bool(fam.get("isLowLatency")),
                subscription=bool(fam.get("subscription")),
                subscription_period=fam.get("subscription_period"),
                networks=list(fam.get("networks") or []),
                has_ads=bool(fam.get("hasAds")),
                pay_as_you_go=bool(fam.get("payAsYouGo")),
                new_user_only=bool(fam.get("newUserOnly")),
                notes=_notes(row),
                source_slug=source_slug,
            )
        )
    return result


def _optional_num(x: Any) -> float | None:
    if x is None:
        return None
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _optional_bool(x: Any) -> bool | None:
    if x is None:
        return None
    return bool(x)


def fetch_country_plans(
    client: httpx.Client,
    slug: str,
    data_dir: Path,
    use_cache: bool = True,
) -> tuple[list[Plan], dict[str, Any]]:
    url = config.plans_endpoint(slug)
    payload, manifest = http.fetch_json(client, url, data_dir, use_cache)
    return parse_folded(payload, source_slug=slug), manifest
