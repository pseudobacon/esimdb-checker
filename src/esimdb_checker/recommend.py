"""Build a recommendation report for an itinerary.

Coverage model (authoritative): a plan is verified to cover a destination if it
is listed on that destination's page. So a plan is eligible for an itinerary if
its id appears on *every* itinerary destination's page — i.e. it is in the
intersection of the per-destination plan id sets. This is esimdb.com's own
coverage assertion and is far more reliable than parsing plan names. Plan-name
coverage (coverage.py) is used only for labeling and to flag ambiguous scopes.

Pipeline: fetch each destination's plans -> intersect id sets -> drop speed-cap
plans (required, with reasons) -> drop unpurchasable -> rank survivors -> emit a
Report (winners, excluded, unverified-coverage, totals, provenance).
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import httpx

from . import catalog, config, coverage as cov_mod, filters, geo
from .models import Itinerary, Plan, Recommendation, Report


class RecommendationEngine:
    def __init__(
        self,
        client: httpx.Client,
        data_dir: Path,
        use_cache: bool = True,
        top_n: int = 5,
        provider: Optional[str] = None,
        include_capped: bool = False,
    ) -> None:
        self.client = client
        self.data_dir = data_dir
        self.use_cache = use_cache
        self.top_n = top_n
        self.provider = provider.lower() if provider else None
        self.include_capped = include_capped
        self.geo: Optional[geo.Geo] = None
        self.plans_by_slug: dict[str, list[Plan]] = {}
        self.plan_ids_by_slug: dict[str, set[str]] = {}
        self.plan_by_id: dict[str, Plan] = {}
        self.plan_slugs: dict[str, set[str]] = {}
        self.manifests: dict[str, dict[str, Any]] = {}
        self.source_urls: list[str] = []

    # ---- data loading -----------------------------------------------------
    def load(self, itinerary: Itinerary) -> None:
        self.geo = geo.Geo.load(self.client, self.data_dir, self.use_cache)
        self.source_urls.extend(
            [config.countries_endpoint(), config.regions_endpoint()]
        )
        self.manifests.update(self.geo.manifests)

        for slug in itinerary.slugs:
            plans, manifest = catalog.fetch_country_plans(
                self.client, slug, self.data_dir, self.use_cache
            )
            self.plans_by_slug[slug] = plans
            self.plan_ids_by_slug[slug] = {p.id for p in plans}
            self.manifests[config.plans_endpoint(slug)] = manifest
            self.source_urls.append(config.plans_endpoint(slug))
            self.source_urls.append(config.destination_page(slug))

        self.plan_by_id = {}
        self.plan_slugs = {}
        for slug, plans in self.plans_by_slug.items():
            for p in plans:
                if p.id not in self.plan_by_id:
                    self.plan_by_id[p.id] = p
                    self.plan_slugs[p.id] = set()
                self.plan_slugs[p.id].add(slug)

    # ---- recommendation ---------------------------------------------------
    def recommend(self, itinerary: Itinerary) -> Report:
        assert self.geo is not None, "call load() first"
        it_names = itinerary.names
        pool_ids = set(self.plan_by_id)
        for slug in itinerary.slugs:
            pool_ids &= self.plan_ids_by_slug.get(slug, set())

        if self.provider:
            pool_ids = {
                pid
                for pid in pool_ids
                if self.plan_by_id[pid].provider.lower() == self.provider
            }

        excluded_speed_cap: list[dict[str, Any]] = []
        unpurchasable = 0
        candidates: list[dict[str, Any]] = []
        unverified: list[dict[str, Any]] = []

        for pid in pool_ids:
            plan = self.plan_by_id[pid]
            cap_reasons = filters.speed_cap_reasons(plan)
            if cap_reasons and not self.include_capped:
                excluded_speed_cap.append(
                    {
                        "id": pid,
                        "name": plan.name,
                        "provider": plan.provider,
                        "usd_price": _round(plan.usd_price),
                        "reasons": cap_reasons,
                        "listed_on": sorted(self.plan_slugs.get(pid, ())),
                    }
                )
                continue
            ok, caveats = filters.purchasable(plan)
            if not ok:
                unpurchasable += 1
                continue
            capped = bool(cap_reasons)
            if capped:
                caveats.append("speed-capped: " + "; ".join(cap_reasons))
            cov = cov_mod.resolve_coverage(plan.name, self.geo)
            meets = filters.meets_data_need_for_days(plan, itinerary.data_gb, itinerary.days)
            reasons_text: list[str] = [
                f"covers all itinerary countries ({', '.join(it_names)})"
            ]
            # Cross-check: name scope vs itinerary.
            if cov.codes and any(code not in cov.codes for code in itinerary.codes):
                missing = [c for c in itinerary.codes if c not in cov.codes]
                caveats.append(
                    "plan name scope may not list "
                    + ", ".join(missing)
                    + " (still listed on those destination pages)"
                )
            if cov.status == "unverified":
                caveats.append(
                    "plan name does not state scope; verified via site listing on "
                    f"all {len(itinerary.slugs)} destination page(s)"
                )
            elif cov.status == "partial":
                caveats.append("name suggests a narrower/ambiguous scope (see label)")
            if plan.unlimited:
                reasons_text.append("unlimited data")
            if plan.data_cap_per == "day":
                reasons_text.append(
                    f"daily allowance: {plan.data_cap_label()} (resets each day)"
                )
            period = plan.period_days
            validity_ok = (period == 0) or (period >= itinerary.days)
            if (not validity_ok) and period > 0:
                caveats.append(
                    f"valid only {period} day(s) but trip is {itinerary.days} — may expire mid-trip"
                )
            candidates.append(
                {
                    "plan": plan,
                    "coverage": cov,
                    "meets": meets,
                    "validity_ok": validity_ok,
                    "capped": capped,
                    "caveats": caveats,
                    "usd": plan.usd_price,
                    "rank_usd": plan.usd_ranking_price(),
                    "per_gb": plan.price_per_gb_usd(),
                    "reasons": reasons_text,
                }
            )
            if cov.status in ("unverified", "partial"):
                unverified.append(
                    {
                        "id": pid,
                        "name": plan.name,
                        "provider": plan.provider,
                        "status": cov.status,
                        "label": cov.label,
                        "note": cov.note,
                    }
                )

        # ---- ranking -------------------------------------------------------
        # Tier 0: meets the data need AND validity spans the trip.
        # Tier 1: meets the data need but validity is short (may expire mid-trip).
        # Tier 2: does not meet the data need (budget fallbacks).
        def tier(c: dict[str, Any]) -> int:
            if c["meets"] and c["validity_ok"]:
                return 0
            if c["meets"]:
                return 1
            return 2

        def sort_key(c: dict[str, Any]):
            period = c["plan"].period_days
            return (
                tier(c),
                c["capped"],  # uncapped plans rank above capped at equal tier/price
                c["rank_usd"] if c["rank_usd"] is not None else float("inf"),
                c["per_gb"] if c["per_gb"] is not None else float("inf"),
                -c["plan"].provider_popularity,
                0 if period == 0 else -period,  # prefer longer validity at equal price
                0 if c["plan"].unlimited else -(c["plan"].capacity_mb),
            )

        candidates.sort(key=sort_key)

        meeting = [c for c in candidates if c["meets"]]
        tier0 = [c for c in meeting if c["validity_ok"]]

        def _best_by(keyfn):
            vals = [c for c in tier0 if keyfn(c) is not None]
            return min(vals, key=keyfn) if vals else None

        cheapest = _best_by(lambda c: c["rank_usd"])
        best_value = _best_by(lambda c: c["per_gb"])
        unlimited_pick = next((c for c in tier0 if c["plan"].unlimited), None)

        # Selection: lead with distinct providers (best plan per provider), then
        # fill remaining slots with the next-best plans (may repeat a provider).
        selected: list[dict[str, Any]] = []
        seen_prov: set[str] = set()
        for c in candidates:
            prov = c["plan"].provider.lower()
            if prov not in seen_prov:
                selected.append(c)
                seen_prov.add(prov)
                if len(selected) >= self.top_n:
                    break
        if len(selected) < self.top_n:
            chosen_ids = {id(c) for c in selected}
            for c in candidates:
                if id(c) not in chosen_ids:
                    selected.append(c)
                    if len(selected) >= self.top_n:
                        break

        winners: list[Recommendation] = []
        for i, c in enumerate(selected, start=1):
            plan = c["plan"]
            reasons = list(c["reasons"])
            tags: list[str] = []
            if cheapest and plan.id == cheapest["plan"].id:
                tags.append("cheapest plan that meets your data need and spans the trip")
            if best_value and plan.id == best_value["plan"].id:
                tags.append("best $/GB among plans that meet your data need")
            if unlimited_pick and plan.id == unlimited_pick["plan"].id:
                tags.append("unlimited data option")
            reasons = tags + reasons
            display_price, currency = plan.price_and_currency(itinerary.currency)
            winners.append(
                Recommendation(
                    rank=i,
                    plan=plan,
                    coverage=c["coverage"],
                    usd_price=_round(plan.usd_price),
                    display_price=_round(display_price),
                    currency=currency or "USD",
                    price_per_gb=_round(plan.price_per_gb_usd()),
                    meets_data_need=c["meets"],
                    reasons=reasons,
                    caveats=list(c["caveats"]),
                    url=config.provider_page(plan.source_slug or itinerary.slugs[0], plan.provider_slug)
                    if plan.provider_slug
                    else config.destination_page(itinerary.slugs[0]),
                )
            )

        fallback = self._fallback(itinerary) if not winners else []

        totals = {
            "destinations": len(itinerary.slugs),
            "unique_plans_scanned": len(self.plan_by_id),
            "pool_covering_all_destinations": len(pool_ids),
            "speed_cap_excluded": len(excluded_speed_cap),
            "unpurchasable_excluded": unpurchasable,
            "eligible_candidates": len(candidates),
            "meeting_data_need": len(meeting),
            "winners": len(winners),
            "unverified_or_partial_name_coverage": len(unverified),
        }

        notes = [
            "Read-only research tool: no purchases, logins, or activations.",
            "Data source: esimdb.com observed client JSON API (not a documented/stable public API).",
            "Coverage is verified by a plan being listed on every itinerary destination page.",
            "Speed-cap plans are excluded by design; reasons are listed in excluded_speed_cap.",
            "Prices are as listed on the site at capture time and may change; confirm before purchase.",
            f"Requested currency: {itinerary.currency}; USD shown where the currency is unavailable.",
        ]

        return Report(
            itinerary=itinerary,
            generated_at=datetime.now(timezone.utc).isoformat(),
            source_urls=dedup(self.source_urls),
            snapshot_manifest={
                url: {
                    "path": str(self.data_dir / _key_path(m["url"])),
                    "sha256": m.get("sha256"),
                    "fetched_at": m.get("fetched_at"),
                    "bytes": m.get("bytes"),
                    "source": m.get("source"),
                }
                for url, m in self.manifests.items()
            },
            totals=totals,
            winners=winners,
            excluded_speed_cap=excluded_speed_cap[:50],
            unverified_coverage=unverified[:50],
            fallback=fallback,
            notes=notes,
        )

    # ---- fallback when nothing covers the whole itinerary -----------------
    def _fallback(self, itinerary: Itinerary) -> list[dict[str, Any]]:
        slug_name = {c.slug: c.name for c in (self.geo.countries if self.geo else [])}
        out: list[dict[str, Any]] = []
        for slug in itinerary.slugs:
            plans = self.plans_by_slug.get(slug, [])
            best: Optional[Plan] = None
            for p in plans:
                if filters.speed_cap_reasons(p):
                    continue
                ok, _ = filters.purchasable(p)
                if not ok:
                    continue
                best = p
                break
            if best is None:
                continue
            out.append(
                {
                    "destination": slug_name.get(slug, slug),
                    "slug": slug,
                    "plan": best.name,
                    "provider": best.provider,
                    "usd_price": _round(best.usd_price),
                    "note": "best purchasable non-speed-capped plan on this destination page",
                }
            )
        return out


def _round(x: Optional[float]) -> Optional[float]:
    if x is None:
        return None
    r = round(float(x), 2)
    return int(r) if r == int(r) else r


def dedup(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for i in items:
        if i not in seen:
            seen.add(i)
            out.append(i)
    return out


def _key_path(url: str) -> str:
    from . import snapshots

    return snapshots.url_key(url) + ".json"
