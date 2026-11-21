"""Recommendation engine: compare COMPLETE strategies for a per-leg itinerary.

Coverage model (authoritative): a plan covers a leg if it is listed on that leg's
destination page (esimdb.com's own coverage assertion). Name-scope parsing is used
only for labelling.

A *strategy* is a complete set of purchases that satisfies every leg:
  - local_only : each leg is served by its own local plan (cheapest feasible per leg);
  - whole_trip : one plan covers every leg (shared data pool, continuous validity);
  - hybrid     : one regional plan serves a subset of whole legs, and local plans
                 serve the rest.
Shared regional data is counted once against the sum of the legs it serves (never
per-country). A plan's validity must span the travel days of every leg it serves.
The primary recommendation is the cheapest *price-comparable* feasible strategy;
provider diversity is not used to override it.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from . import catalog, config, filters, geo
from . import fx as fx_mod
from .models import Fx, Leg, Plan, Report, Strategy, StrategyPlan, Trip

FAMILY_ORDER = ["local_only", "whole_trip", "hybrid"]

# Multi-destination plans are tried against every subset of the legs they are listed
# on up to this many legs (2^n work); wider trips fall back to contiguous travel
# windows and say so in the report instead of quietly searching less.
_MAX_SUBSET_LEGS = 8


class RecommendationEngine:
    def __init__(
        self,
        client: httpx.Client,
        data_dir: Path,
        use_cache: bool = True,
        include_capped: bool = False,
        provider: str | None = None,
        regional_candidate_limit: int = 60,
        speed_verified: bool = False,
        speed_note: str | None = None,
        strict_speed_cap: bool = False,
    ) -> None:
        self.client = client
        self.data_dir = data_dir
        self.use_cache = use_cache
        self.include_capped = include_capped
        # Speed policy. Default: exclude plans throttled *from the first byte*, keep
        # plans that throttle only once the allowance is spent - a plan that already
        # meets the trip's data need can never reach that throttle. strict_speed_cap
        # restores the old rule (exclude both); include_capped admits everything.
        self.strict_speed_cap = strict_speed_cap
        self.provider = provider.lower() if provider else None
        self.regional_candidate_limit = regional_candidate_limit
        self.speed_verified = speed_verified
        self.speed_note = speed_note
        self.geo: geo.Geo | None = None
        self.fx: Fx | None = None
        self.plans_by_slug: dict[str, list[Plan]] = {}
        self.plan_ids_by_slug: dict[str, set[str]] = {}
        self.plan_by_id: dict[str, Plan] = {}
        self.plan_slugs: dict[str, set[str]] = {}
        # Multi-destination candidates are computed once and reused by _evaluate,
        # _diagnostics and totals; they depend only on the acquired data and flags.
        self._regional_cache: list[tuple[float, Plan, tuple[int, ...]]] | None = None
        self._regional_truncated = 0
        self._regional_scope_mode = "exhaustive"
        self._regional_windowed_plans = 0
        self.manifests: dict[str, dict[str, Any]] = {}
        self.source_urls: list[str] = []
        self.fetch_status: dict[str, dict[str, Any]] = {}

    # ---- data loading -----------------------------------------------------
    def load(self, trip: Trip, progress: Callable[[int, int, str], None] | None = None) -> None:
        """Fetch everything the report needs.

        ``progress(step, total, label)`` is called before each destination's plan
        fetch, so an interactive front-end can show what it is waiting on.
        """
        # Candidates depend on the legs and the acquired plans, so a reloaded engine must
        # not serve a previous trip's cached leg sets.
        self._regional_cache = None
        self._regional_truncated = 0
        self._regional_scope_mode = "exhaustive"
        self._regional_windowed_plans = 0
        self.geo = geo.Geo.load(self.client, self.data_dir, self.use_cache)
        self.fx = fx_mod.fetch_fx(self.client, trip.currency, self.data_dir, self.use_cache)
        self.source_urls.extend([config.countries_endpoint(), config.regions_endpoint()])
        self.manifests.update(self.geo.manifests)

        self.legs = [(i, leg) for i, leg in enumerate(trip.legs) if not leg.is_transfer]
        self.leg_by_idx = {i: leg for i, leg in self.legs}
        distinct_slugs = list(dict.fromkeys(leg.country.slug for _, leg in self.legs))
        names = {}
        for _, leg in self.legs:
            names.setdefault(leg.country.slug, leg.country.name)
        for step, slug in enumerate(distinct_slugs, 1):
            if progress is not None:
                progress(step, len(distinct_slugs), names.get(slug, slug))
            try:
                plans, manifest = catalog.fetch_country_plans(
                    self.client, slug, self.data_dir, self.use_cache
                )
                self.fetch_status[slug] = {"ok": True, "count": len(plans)}
                self.manifests[config.plans_endpoint(slug)] = manifest
            except Exception as exc:  # noqa: BLE001
                plans = []
                self.fetch_status[slug] = {"ok": False, "count": 0, "error": str(exc)}
            self.plans_by_slug[slug] = plans
            self.plan_ids_by_slug[slug] = {p.id for p in plans}
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

    # ---- predicates -------------------------------------------------------
    def _covers_leg(self, plan: Plan, leg: Leg) -> bool:
        return leg.country.slug in self.plan_slugs.get(plan.id, set())

    def _speed_ok(self, plan: Plan) -> bool:
        """Default: keep plans that throttle only after their allowance is spent - one
        that already meets the trip's data need cannot reach that throttle. See
        `filters.speed_cap_signals` for how the two classes are told apart."""
        if self.include_capped:
            return True
        if self.strict_speed_cap:
            return not filters.speed_cap_reasons(plan)
        return not filters.always_throttled(plan)

    def _provider_ok(self, plan: Plan) -> bool:
        return self.provider is None or plan.provider.lower() == self.provider

    def _purchasable(self, plan: Plan) -> bool:
        ok, _ = filters.purchasable(plan)
        return ok

    def _data_feasible(self, plan: Plan, legs: list[Leg], daily_target: float) -> bool:
        """Shared data feasibility: one pool vs the SUM of the legs' needs."""
        if plan.unlimited:
            return True
        if plan.is_daily_refill():
            return (plan.daily_cap_gb or 0.0) >= daily_target - 1e-9
        need = sum(leg.required_gb for leg in legs)
        return plan.data_gb >= need - 1e-9

    def _validity_info(self, plan: Plan, activation_day: int, legs: list[Leg]) -> tuple[bool, bool]:
        """Return (hard_ok, unverified). hard_ok is False if the plan provably
        expires before some leg ends. unverified is True if any served leg has an
        unknown listed period."""
        hard_ok = True
        unverified = False
        for leg in legs:
            if plan.period_days > 0:
                if activation_day + plan.period_days - 1 < leg.end_day:
                    hard_ok = False
            else:
                unverified = True
        return hard_ok, unverified

    def _assumptions_for(
        self, plan: Plan, legs: list[Leg], activation_day: int, trip: Trip
    ) -> list[str]:
        out: list[str] = []
        # A plan is activated once, at the earliest of the legs it serves (not
        # separately per leg). Only note it when that day is not the trip start.
        if activation_day > 1:
            first = min(legs, key=lambda leg: leg.start_day)
            out.append(
                f"assumes '{plan.name}' is activated at travel day {activation_day} "
                f"(arrival to {first.country.name}); "
                f"activation terms: {plan.activation_type or 'unspecified'}"
            )
        if plan.period_days <= 0:
            lo = min(leg.start_day for leg in legs)
            hi = max(leg.end_day for leg in legs)
            out.append(
                f"assumes '{plan.name}' validity spans travel days "
                f"{lo}\u2013{hi} (listed period unknown)"
            )
        phrase = filters.after_cap_phrase(plan)
        if phrase:
            out.append(
                f"'{plan.name}' is accepted despite a speed cap because the throttle starts only "
                f"once the allowance is spent ({phrase}); the data this strategy needs fits inside "
                f"the full-speed allowance"
            )
        return out

    # ---- candidate generation --------------------------------------------
    def _best_local_plan(self, leg: Leg, trip: Trip) -> tuple[Plan, float, list[str], bool] | None:
        cands: list[tuple[float, bool, Plan]] = []
        for plan in self.plans_by_slug.get(leg.country.slug, []):
            if (not self._speed_ok(plan) or not self._provider_ok(plan)
                    or not self._purchasable(plan)):
                continue
            if not self._data_feasible(plan, [leg], trip.daily_data_gb):
                continue
            hard_ok, unverified = self._validity_info(plan, leg.start_day, [leg])
            if not hard_ok:
                continue
            cost, comparable = plan.price_in(trip.currency, self.fx)
            if not comparable:
                continue
            cands.append((cost, unverified, plan))
        if not cands:
            return None
        cands.sort(key=lambda t: (t[0], t[1], -self.plan_by_id[t[2].id].provider_popularity))
        cost, unverified, plan = cands[0]
        assumptions = self._assumptions_for(plan, [leg], leg.start_day, trip)
        return plan, cost, assumptions, not unverified

    def _regional_candidates(
        self, trip: Trip
    ) -> list[tuple[float, Plan, tuple[int, ...]]]:
        """Cheapest plan per *servable leg set*, memoized.

        Two rules used to be applied at the set of legs a plan happens to be listed
        on: it had to span >= 2 distinct countries, and it had to be data-feasible
        and valid for every one of those legs. Both hid the wrong plans. A 30-day
        50 GB plan listed on all four destination pages of a 32-day trip is not a
        whole-trip plan - but it is a perfectly good plan for the first 30 days of
        it, and the old pre-check deleted it before the subset pass in `_evaluate`
        could discover that. Repeat visits to one country were excluded outright,
        so a trip that passes through the same country twice bought two plans where
        a single longer plan serves both.

        Feasibility is therefore judged per leg set the plan would actually serve,
        and that leg set is the dedupe key. The candidate count is still bounded by
        the number of leg sets, one plan each, so no cost cap can prune the pricier
        plan that serves the whole trip; truncation (only possible on very wide
        trips) is reported rather than silent.
        """
        if self._regional_cache is not None:
            return self._regional_cache
        best: dict[frozenset, tuple[float, Plan, tuple[int, ...]]] = {}
        for plan in self.plan_by_id.values():
            if (not self._speed_ok(plan) or not self._provider_ok(plan)
                    or not self._purchasable(plan)):
                continue
            covered = tuple(i for i, _ in self.legs
                            if self._covers_leg(plan, self.leg_by_idx[i]))
            if len(covered) < 2:
                continue  # one leg only: that is a local pick, not a multi-leg purchase
            cost, comparable = plan.price_in(trip.currency, self.fx)
            if not comparable:
                continue
            rank = (cost, -plan.provider_popularity)
            for scope in self._servable_scopes(plan, covered, trip.daily_data_gb):
                cur = best.get(scope)
                if cur is None or rank < (cur[0], -cur[1].provider_popularity):
                    best[scope] = (cost, plan, tuple(sorted(scope)))
        cands = sorted(
            best.values(),
            key=lambda t: (-len(t[2]), t[0], -t[1].provider_popularity),
        )
        self._regional_scope_mode = (
            "exhaustive" if not self._regional_windowed_plans
            else "contiguous_windows_for_wide_plans"
        )
        self._regional_truncated = max(0, len(cands) - self.regional_candidate_limit)
        self._regional_cache = cands[: self.regional_candidate_limit]
        return self._regional_cache

    def _servable_scopes(
        self, plan: Plan, covered: tuple[int, ...], daily_data_gb: float
    ) -> list[frozenset]:
        """Maximal subsets of `covered` this plan can serve (data + validity).

        Widen a leg set and its data need only grows while the span the validity has
        to reach only lengthens, so every servable set sits under a maximal one and
        only those need keeping: `_evaluate` re-enumerates the subsets of each and
        prices them at the same cost, and a single-leg set is what `_best_local_plan`
        already finds per leg. Above `_MAX_SUBSET_LEGS` legs the search is restricted
        to contiguous travel windows, which is disclosed in the report.
        """
        if self._serves(plan, [self.leg_by_idx[i] for i in covered], daily_data_gb):
            return [frozenset(covered)]
        n = len(covered)
        if n > _MAX_SUBSET_LEGS:
            self._regional_windowed_plans += 1
            sets: list[tuple[int, ...]] = [
                covered[a:b + 1] for a in range(n) for b in range(a + 1, n)
            ]
        else:
            sets = [tuple(s) for s in self._subsets(list(covered)) if len(s) >= 2]
        ok = []
        for S in sets:
            if self._serves(plan, [self.leg_by_idx[i] for i in S], daily_data_gb):
                ok.append(frozenset(S))
        return [S for S in ok if not any(S < T for T in ok)]

    def _serves(self, plan: Plan, legs: list[Leg], daily_data_gb: float) -> bool:
        """One leg set: enough data for those legs, and still alive when the last ends."""
        if not self._data_feasible(plan, legs, daily_data_gb):
            return False
        act = min(leg.start_day for leg in legs)
        return self._validity_info(plan, act, legs)[0]

    @staticmethod
    def _subsets(seq: list[int] | tuple[int, ...]) -> list[list[int]]:
        n = len(seq)
        out = []
        for mask in range(1, 1 << n):
            out.append([seq[j] for j in range(n) if mask & (1 << j)])
        return out

    # ---- strategy construction -------------------------------------------
    @staticmethod
    def _make_strategy(
        family: str, plans: list[StrategyPlan], total: float, trip: Trip, assumptions: list[str]
    ) -> Strategy:
        validity_ok = all(p.plan.period_days > 0 for p in plans)
        return Strategy(
            family=family,
            plans=tuple(plans),
            total_cost=total,
            currency=trip.currency,
            comparable=True,
            meets_data=True,
            validity_ok=validity_ok,
            assumptions=_dedupe(assumptions),
        )

    def _evaluate(self, trip: Trip) -> dict[str, Strategy | None]:
        all_idx = [i for i, _ in self.legs]
        local_picks: dict[int, tuple[Plan, float, list[str], bool] | None] = {}
        for i, leg in self.legs:
            local_picks[i] = self._best_local_plan(leg, trip)

        strategies: dict[str, Strategy | None] = {
            "local_only": None,
            "whole_trip": None,
            "hybrid": None,
        }

        # local-only
        if all(local_picks[i] is not None for i in all_idx):
            plans: list[StrategyPlan] = []
            assumptions: list[str] = []
            total = 0.0
            for i in all_idx:
                plan, cost, assump, _ = local_picks[i]
                plans.append(
                    StrategyPlan(
                        plan=plan, legs=(i,), activation_day=self.leg_by_idx[i].start_day,
                        cost=cost, comparable=True, assumptions=assump,
                    )
                )
                assumptions += assump
                total += cost
            strategies["local_only"] = self._make_strategy(
                "local_only", plans, total, trip, assumptions
            )

        # whole-trip + hybrid from regional candidates
        for cost, plan, covered in self._regional_candidates(trip):
            for S in self._subsets(covered):
                if not S:
                    continue
                act = min(self.leg_by_idx[i].start_day for i in S)
                s_legs = [self.leg_by_idx[i] for i in S]
                if not self._data_feasible(plan, s_legs, trip.daily_data_gb):
                    continue
                hard_ok, _ = self._validity_info(plan, act, s_legs)
                if not hard_ok:
                    continue
                rest = [i for i in all_idx if i not in S]
                if any(local_picks[i] is None for i in rest):
                    continue
                plans = [
                    StrategyPlan(
                        plan=plan, legs=tuple(sorted(S)), activation_day=act,
                        cost=cost, comparable=True,
                        assumptions=self._assumptions_for(plan, s_legs, act, trip),
                    )
                ]
                assumptions = list(plans[0].assumptions)
                total = cost
                for i in rest:
                    lplan, lcost, lassump, _ = local_picks[i]
                    plans.append(
                        StrategyPlan(
                            plan=lplan, legs=(i,), activation_day=self.leg_by_idx[i].start_day,
                            cost=lcost, comparable=True, assumptions=lassump,
                        )
                    )
                    assumptions += lassump
                    total += lcost
                family = "whole_trip" if set(S) == set(all_idx) else "hybrid"
                cand = self._make_strategy(family, plans, total, trip, assumptions)
                cur = strategies[family]
                if cur is None or _less(cand, cur):
                    strategies[family] = cand

        return strategies

    # ---- budget -----------------------------------------------------------
    @staticmethod
    def _budget_status(recommended: Strategy | None, trip: Trip) -> dict[str, Any]:
        budget = trip.budget
        cost = recommended.total_cost if recommended else None
        if budget is None:
            return {
                "budget": None,
                "currency": trip.currency,
                "cost": cost,
                "within_original": True if cost is not None else False,
                "relaxed": False,
                "steps": 0,
                "threshold": None,
                "overspend": None,
                "message": (
                    "No budget specified." if cost is not None
                    else "No budget specified and no feasible strategy."
                ),
            }
        if cost is None:
            return {
                "budget": budget,
                "currency": trip.currency,
                "cost": None,
                "within_original": False,
                "relaxed": False,
                "steps": 0,
                "threshold": None,
                "overspend": None,
                "message": "No feasible strategy to compare against the budget.",
            }
        if cost <= budget:
            return {
                "budget": budget,
                "currency": trip.currency,
                "cost": cost,
                "within_original": True,
                "relaxed": False,
                "steps": 0,
                "threshold": budget,
                "overspend": 0.0,
                "message": f"Within the original budget of {_money(budget, trip.currency)}.",
            }
        # relax: compound 10% until budget >= cost
        steps = 0
        threshold = budget
        while threshold < cost and steps < 1000:
            threshold *= 1.1
            steps += 1
        overspend = cost - budget
        msg = (
            f"No eSIM strategy meeting your data requirement was found within your "
            f"original budget of {_money(budget, trip.currency)}. These results are "
            f"outside your budget cap."
        )
        return {
            "budget": budget,
            "currency": trip.currency,
            "cost": cost,
            "within_original": False,
            "relaxed": steps > 0,
            "steps": steps,
            "threshold": threshold,
            "overspend": overspend,
            "message": msg,
        }

    # ---- diagnostics -------------------------------------------------------
    def _diagnostics(
        self,
        trip: Trip,
        strategies: dict[str, Strategy | None],
        local_picks: dict[int, Any | None],
        recommended: Strategy | None,
    ) -> dict[str, Any]:
        """Bucket each covering plan under the reason that actually rules it out.

        A plan is counted once, in the order the checks that would keep it out even if
        the *next* filter were relaxed: provider filter -> purchasability -> data need
        -> speed cap. So a "1 Day / Daily 500MB" plan on a 1.5 GB/day trip is reported
        as data-insufficient, not as a cap exclusion: relaxing the cap with
        --include-capped could never admit it, and listing it under the cap made the
        exclusions look like candidates that only the strict filter was hiding.

        The cap bucket holds only plans throttled *from the first byte* (explicit
        speedLimit, or the site's possibleThrottling flag). A plan that throttles only
        once its allowance is spent (reducedSpeed) is a normal candidate, so it is
        counted as admitted - or, under --strict-speed-cap, as excluded.

        Two multi-destination buckets ride along, because a three-line summary cannot
        answer "why was this specific plan not bought?": `regional_near_matches` for
        plans that clear every filter but the calendar, and `regional_alternatives`
        for plans that are genuinely feasible for part of the trip but were not the
        cheapest purchase the recommendation made.
        """
        excluded_always_throttled: list[dict[str, Any]] = []
        throttled_total = 0
        data_total = 0
        unpurchasable = 0
        off_provider = 0
        after_cap_admitted = 0
        after_cap_excluded_strict = 0
        seen_throttled: set[str] = set()
        regional_near: list[dict[str, Any]] = []
        regional_near_total = 0
        for pid, plan in self.plan_by_id.items():
            covered = [lg for i, lg in self.legs
                       if self.leg_by_idx[i].country.slug in self.plan_slugs.get(pid, set())]
            if not covered:
                continue
            if not self._provider_ok(plan):
                off_provider += 1
                continue
            if not self._purchasable(plan):
                unpurchasable += 1
                continue
            # Usable at all => it could serve at least one of the legs it covers.
            if not any(self._data_feasible(plan, [lg], trip.daily_data_gb)
                       for lg in covered):
                # A pre-filter, not a diagnostic: this plan was never a candidate for
                # this trip, so it is counted and left out of the listing. The
                # speed-cap bucket stays enumerable - those plans *were* viable and
                # only the strict cap removed them.
                data_total += 1
                continue
            hard, after = filters.speed_cap_signals(plan)
            if hard and not self.include_capped:
                throttled_total += 1
                if pid not in seen_throttled and len(excluded_always_throttled) < _DETAIL_CAP:
                    price, _ = plan.price_in(trip.currency, self.fx)
                    excluded_always_throttled.append(
                        {
                            "id": pid,
                            "name": plan.name,
                            "provider": plan.provider,
                            "price": _round(price),
                            "currency": trip.currency,
                            "reasons": hard,
                        }
                    )
                    seen_throttled.add(pid)
                continue
            elif after:
                if self.strict_speed_cap:
                    after_cap_excluded_strict += 1
                else:
                    after_cap_admitted += 1

            # Enough data for every leg it is listed on, but the listed period runs
            # out first. This is the "why isn't one plan enough for my whole trip?"
            # answer, and it used to be invisible: such plans reached no bucket at all.
            distinct = {lg.country.name for lg in covered}
            if (len(covered) >= 2 and len(distinct) >= 2 and plan.period_days > 0
                    and self._data_feasible(plan, covered, trip.daily_data_gb)):
                act = min(lg.start_day for lg in covered)
                cost, comparable = plan.price_in(trip.currency, self.fx)
                # Only price-comparable plans can be weighed against the recommendation,
                # so an unpriceable one is not worth a line here (it can never be bought
                # in this currency either).
                full_scope_ok, _ = self._validity_info(plan, act, covered)
                if comparable and cost is not None and not full_scope_ok:
                    last = max(lg.end_day for lg in covered)
                    regional_near_total += 1
                    regional_near.append({
                        "id": pid,
                        "name": plan.name,
                        "provider": plan.provider,
                        "price": _round(cost),
                        "currency": trip.currency,
                        "legs": [lg.country.name for lg in covered],
                        "period_days": plan.period_days,
                        "covers_days": f"{act}-{act + plan.period_days - 1}",
                        "legs_run_to_day": last,
                        "short_by_days": last - (act + plan.period_days - 1),
                        "reason": (
                            f"listed period {plan.period_days} days ends travel day "
                            f"{act + plan.period_days - 1}, {last - (act + plan.period_days - 1)} "
                            f"day(s) before the last leg it is listed on ends"
                        ),
                    })

        # near-matches: per leg, the cheapest purchasable, non-speed-excluded plan that
        # is data-feasible but provably expires before the leg ends.
        near_matches: list[dict[str, Any]] = []
        for _i, leg in self.legs:
            best_cost: float | None = None
            best_plan: Plan | None = None
            for plan in self.plans_by_slug.get(leg.country.slug, []):
                if not self._provider_ok(plan) or not self._purchasable(plan):
                    continue
                if not self._speed_ok(plan):
                    continue
                if not self._data_feasible(plan, [leg], trip.daily_data_gb):
                    continue
                hard_ok, _ = self._validity_info(plan, leg.start_day, [leg])
                if hard_ok:
                    continue
                cost, comparable = plan.price_in(trip.currency, self.fx)
                if not comparable:
                    continue
                if best_cost is None or cost < best_cost:
                    best_cost = cost
                    best_plan = plan
            if best_plan is not None:
                near_matches.append(
                    {
                        "leg": leg.country.name,
                        "plan": best_plan.name,
                        "provider": best_plan.provider,
                        "price": _round(best_cost),
                        "currency": trip.currency,
                        "reason": "expires before the leg ends (listed period too short)",
                    }
                )

        # Feasible multi-destination purchases the recommendation did not make. The
        # strategy table shows one winner per family, so every other plan that would
        # genuinely work was previously absent from the report entirely.
        # Closest to working first: a plan one day short is worth reading about, one
        # 30 days short is not.
        regional_near.sort(key=lambda e: (e["short_by_days"], e["price"], e["name"]))
        used_ids = {sp.plan.id for s in list(strategies.values()) + [recommended]
                    if s is not None for sp in s.plans}
        alternatives: list[dict[str, Any]] = []
        alt_total = 0
        # Most of the trip covered first, then cheapest: the interesting question is
        # "what else could one plan do?", not "what is the cheapest pair of legs?".
        for cost, plan, scope in sorted(self._regional_candidates(trip),
                                        key=lambda t: (-len(t[2]), t[0], t[1].name)):
            if plan.id in used_ids:
                continue
            alt_total += 1
            if len(alternatives) < _DETAIL_CAP:
                alternatives.append({
                    "id": plan.id,
                    "name": plan.name,
                    "provider": plan.provider,
                    "price": _round(cost),
                    "currency": trip.currency,
                    "legs": [self.leg_by_idx[i].country.name for i in scope],
                    "covers_days": (
                        f"{min(self.leg_by_idx[i].start_day for i in scope)}"
                        f"-{max(self.leg_by_idx[i].end_day for i in scope)}"
                    ),
                    "period_days": plan.period_days,
                    "data_gb": None if plan.unlimited else round(plan.data_gb, 2),
                })

        # unservable legs: no local pick and not coverable by any feasible multi-leg plan
        regional_covered: set[int] = set()
        for _, _plan, covered in self._regional_candidates(trip):
            regional_covered.update(covered)
        unservable = []
        for i, leg in self.legs:
            if local_picks.get(i) is None and i not in regional_covered:
                unservable.append(leg.country.name)

        fetch_failed = [s for s, st in self.fetch_status.items() if not st["ok"]]
        return {
            "excluded_always_throttled": excluded_always_throttled,
            "excluded_always_throttled_total": throttled_total,
            "after_cap_admitted": after_cap_admitted,
            "after_cap_excluded_strict": after_cap_excluded_strict,
            "data_insufficient_filtered": data_total,
            "unpurchasable_excluded": unpurchasable,
            "provider_filtered_out": off_provider,
            "near_matches": near_matches[:20],
            "regional_near_matches": regional_near[:_DETAIL_CAP],
            "regional_near_matches_total": regional_near_total,
            "regional_alternatives": alternatives,
            "regional_alternatives_total": alt_total,
            "unservable_legs": unservable,
            "fetch_failed_slugs": fetch_failed,
        }

    def _no_solution_reason(
        self, trip: Trip, strategies: dict[str, Strategy | None], diag: dict[str, Any]
    ) -> str | None:
        if any(strategies[k] is not None for k in FAMILY_ORDER):
            return None
        if diag["fetch_failed_slugs"]:
            return (
                "plan acquisition failed or was incomplete for: "
                + ", ".join(diag["fetch_failed_slugs"])
            )
        if diag["unservable_legs"]:
            return (
                "no plan in the acquired data can serve leg(s) "
                + ", ".join(diag["unservable_legs"])
                + " at the required data/validity — no compliant strategy at any price"
            )
        return (
            "no price-comparable feasible strategy in the acquired data "
            "(check currency/FX availability)"
        )

    # ---- main -------------------------------------------------------------
    def recommend(self, trip: Trip) -> Report:
        assert self.geo is not None, "call load() first"
        local_picks = {i: self._best_local_plan(leg, trip) for i, leg in self.legs}
        strategies = self._evaluate(trip)
        # The recommendation is chosen first: the diagnostics need to know which plans
        # it bought, so that "feasible but not chosen" can be reported instead of lost.
        feasible = [s for s in strategies.values() if s is not None and s.comparable]
        recommended: Strategy | None = None
        if feasible:
            recommended = min(
                feasible,
                key=lambda s: (s.total_cost, s.purchase_count, FAMILY_ORDER.index(s.family)),
            )

        diag = self._diagnostics(trip, strategies, local_picks, recommended)
        no_solution = self._no_solution_reason(trip, strategies, diag)
        budget_status = self._budget_status(recommended, trip)

        # fill plan URLs for every reported strategy
        base_slug = self.legs[0][1].country.slug if self.legs else None
        for s in list(strategies.values()) + ([recommended] if recommended else []):
            if s is None:
                continue
            for sp in s.plans:
                if base_slug is None:
                    sp.url = None
                elif sp.plan.provider_slug:
                    sp.url = config.provider_page(
                        sp.plan.source_slug or base_slug, sp.plan.provider_slug
                    )
                else:
                    sp.url = config.destination_page(base_slug)

        totals = {
            "legs": len(trip.legs),
            "distinct_countries": len(trip.distinct_countries),
            "unique_plans_scanned": len(self.plan_by_id),
            "plans_covering_all_legs": sum(
                1
                for pid in self.plan_by_id
                if all(
                    self.leg_by_idx[i].country.slug in self.plan_slugs.get(pid, set())
                    for i, _ in self.legs
                )
            ),
            "always_throttled_excluded": diag["excluded_always_throttled_total"],
            "after_cap_admitted": diag["after_cap_admitted"],
            "data_insufficient_filtered": diag["data_insufficient_filtered"],
            "unpurchasable_excluded": diag["unpurchasable_excluded"],
            "regional_candidates_considered": len(self._regional_candidates(trip)),
            "regional_candidates_truncated": self._regional_truncated,
            "regional_scope_search": self._regional_scope_mode,
            "feasible_families": [k for k in FAMILY_ORDER if strategies[k] is not None],
        }

        notes = [
            "Read-only research tool: no purchases, logins, or activations.",
            "Data source: esimdb.com observed client JSON API "
            "(not a documented/stable public API).",
            "Coverage verified by a plan being listed on every leg's destination page.",
            "Prices are as listed at capture time and may change; confirm before purchase.",
            f"Requested currency: {trip.currency}; ranking, budget, and totals "
            "all use this currency.",
        ]
        if no_solution:
            notes.insert(0, f"No feasible strategy: {no_solution}.")
        if self._regional_windowed_plans:
            notes.append(
                f"{self._regional_windowed_plans} plans are listed on more than "
                f"{_MAX_SUBSET_LEGS} legs, so they were tried against contiguous travel "
                f"windows rather than every leg combination."
            )
        if self._regional_truncated:
            notes.append(
                f"{self._regional_truncated} multi-destination leg combinations were dropped by "
                f"the engine's candidate limit ({self.regional_candidate_limit}); some may be "
                f"cheaper than what was recommended."
            )
        if diag["regional_alternatives_total"]:
            notes.append(
                f"{diag['regional_alternatives_total']} further multi-leg plans would "
                f"work for part of this trip but cost more than the recommendation - listed "
                f"under diagnostics."
            )
        fx_note = self.fx.note if self.fx else ""
        if fx_note:
            notes.append(f"FX: {fx_note}")

        speed_status = self._speed_filter_status(trip)

        return Report(
            trip=trip,
            generated_at=datetime.now(timezone.utc).isoformat(),
            source_urls=_dedupe(self.source_urls),
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
            fx=self.fx.to_dict() if self.fx else {"available": False},
            recommended=recommended,
            strategies=strategies,
            budget_status=budget_status,
            diagnostics=diag,
            totals=totals,
            speed_filter_status=speed_status,
            notes=notes,
        )

    def _speed_filter_status(self, trip: Trip) -> dict[str, Any]:
        policy = "strict_all_speed_caps" if self.strict_speed_cap else "always_throttled_only"
        if self.speed_verified:
            return {
                "requirement": "uncapped eSIMs (esimdb built-in 'Hide plans with speed caps')",
                "mode": "verified_site_toggle",
                "verified": True,
                "include_capped": self.include_capped,
                "strict_speed_cap": self.strict_speed_cap,
                "policy": policy,
                "note": self.speed_note or "verified against the site's built-in speed-cap toggle",
            }
        note = (
            "Speed-cap handling uses a LOCAL PREDICATE on the API's speed fields. Only plans "
            "throttled from the first byte (speedLimit, possibleThrottling) are excluded: a plan "
            "that throttles only once its allowance is spent (reducedSpeed) stays a candidate, "
            "because a plan that already meets the trip's data need cannot reach that throttle. "
            "This is a safeguard, NOT the site's built-in 'Hide plans with speed caps' toggle; "
            "that toggle was not exercised."
        )
        if self.strict_speed_cap:
            note += (
                "  --strict-speed-cap was explicitly set: after-allowance throttles "
                "are excluded too."
            )
        if self.include_capped:
            note += (
                "  --include-capped was explicitly set: speed-capped plans are INCLUDED, "
                "so this run does NOT satisfy the strict uncapped requirement."
            )
        return {
            "requirement": "uncapped eSIMs (esimdb built-in 'Hide plans with speed caps')",
            "mode": "local_predicate",
            "verified": False,
            "include_capped": self.include_capped,
            "strict_speed_cap": self.strict_speed_cap,
            "policy": policy,
            "note": note,
        }


# ---- helpers --------------------------------------------------------------
def _less(a: Strategy, b: Strategy) -> bool:
    return (a.total_cost, a.purchase_count, FAMILY_ORDER.index(a.family)) < (
        b.total_cost,
        b.purchase_count,
        FAMILY_ORDER.index(b.family),
    )


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for i in items:
        if i not in seen:
            seen.add(i)
            out.append(i)
    return out


def _round(x: float | None) -> float | None:
    if x is None:
        return None
    r = round(float(x), 2)
    return int(r) if r == int(r) else r


# How many excluded plans get a line of detail; the counts reported alongside are the
# true totals, so a capped list can never be mistaken for "there were exactly 50".
_DETAIL_CAP = 20


def _money(v: float, currency: str) -> str:
    sym = {"EUR": "€", "USD": "$", "GBP": "£"}.get(currency, currency + " ")
    return f"{sym}{v:,.2f}"


def _key_path(url: str) -> str:
    from . import snapshots

    return snapshots.url_key(url) + ".json"
