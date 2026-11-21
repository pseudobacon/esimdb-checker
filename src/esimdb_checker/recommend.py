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

# The purchase-count search tries every split of the legs into purchases, which is
# 3^n states. Past this many legs it keeps only splits into contiguous travel windows
# and says so in the report, rather than either hanging or quietly answering nothing.
_MAX_PARTITION_LEGS = 10

# How long before the first arrival a plan is assumed to be installed. 1, not 0: installing an
# eSIM needs internet, and arriving with none is exactly the case people plan around, so a model
# that installs on arrival prices a trip nobody takes. It costs validity only - the lead-in day is
# not a roaming day - and it is what makes the recommendation move between 4 and 5 eSIMs on a
# multi-country trip. Pass 0 to reproduce pre-2026-11 results.
_DEFAULT_ACTIVATE_LEAD_DAYS = 1


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
        max_purchases: int | None = None,
        fewer_sims_pct: float | None = None,
        activate_lead_days: int = _DEFAULT_ACTIVATE_LEAD_DAYS,
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
        self._partition_search = "exact"
        # Purchase policy: None keeps the historic behaviour of recommending the cheapest
        # strategy whatever it costs in eSIMs. max_purchases is a hard limit; fewer_sims
        # trades price for convenience, up to a tolerance in percent.
        self.max_purchases = max_purchases
        self.fewer_sims_pct = fewer_sims_pct
        # Installation reality: an eSIM is switched on where the internet is known, which is
        # usually the day (or flight) before crossing a border, because arrival with no data
        # means no way to install it. Every validity check below measures from that earlier
        # day, so a plan whose validity exactly matched the road days now falls short.
        # An explicit 0 means "install on arrival" and must survive; only None means "use the
        # product default", which is shared with the CLI so the two cannot disagree.
        lead = (_DEFAULT_ACTIVATE_LEAD_DAYS if activate_lead_days is None
                else int(activate_lead_days))
        self.activate_lead_days = max(0, lead)
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
        self._partition_search = "exact"
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

    def _activation_day(self, legs: list[Leg]) -> int:
        """Travel day a plan gets switched on for this set of legs.

        The earliest arrival among them, moved earlier by ``activate_lead_days``. Travel day 0
        is legal and means "the day before the trip starts" - installing at home on known wifi.
        """
        return min(leg.start_day for leg in legs) - self.activate_lead_days

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
        # A plan is activated once, for the whole set of legs it serves (not separately per
        # leg). Note it whenever that day is not simply the trip's first day.
        first = min(legs, key=lambda leg: leg.start_day)
        lead = first.start_day - activation_day
        if lead > 0 or activation_day > 1:
            when = (
                f"travel day {activation_day}, {lead} day{'s' if lead != 1 else ''} before "
                f"arrival to {first.country.name}" if lead > 0
                else f"travel day {activation_day} (arrival to {first.country.name})"
            )
            out.append(
                f"assumes '{plan.name}' is activated on {when}; "
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
            hard_ok, unverified = self._validity_info(plan, self._activation_day([leg]), [leg])
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
        assumptions = self._assumptions_for(plan, [leg], self._activation_day([leg]), trip)
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
        act = self._activation_day(legs)
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

    # ---- purchase-count frontier ------------------------------------------
    def _base_cover_costs(
        self, trip: Trip, local_picks: dict[int, Any]
    ) -> tuple[dict[int, tuple[float, Any]], dict[int, int], dict[int, int]]:
        """cheapest plan per *exact* leg set, as a sparse mask -> (cost, plan) map.

        Candidates arrive keyed by the maximal leg set they can serve. Singleton sets
        come from the per-leg local picks, which is what `local_only` buys, so the
        frontier's most-expensive row is the same purchase list the old family
        enumeration produced.
        """
        bit = {i: b for b, (i, _) in enumerate(self.legs)}
        leg_of = {b: i for i, b in bit.items()}
        base: dict[int, tuple[float, Any]] = {}
        for cost, plan, scope in self._regional_candidates(trip):
            m = 0
            for i in scope:
                m |= 1 << bit[i]
            cur = base.get(m)
            if cur is None or cost < cur[0]:
                base[m] = (cost, plan)
        for i, _leg in self.legs:
            pick = local_picks.get(i)
            if pick is None:
                continue
            plan, cost, _assumptions, _verified = pick
            m = 1 << bit[i]
            cur = base.get(m)
            if cur is None or cost < cur[0]:
                base[m] = (cost, plan)
        return base, bit, leg_of

    def _dense_cover(
        self, base: dict[int, tuple[float, Any]], n: int
    ) -> tuple[list[float], list[Any]]:
        """cheapest plan that can serve *at least* a leg set, for every one of 2^n sets.

        A plan bought for a set can serve any subset of it - the data need is a sum over
        the set so it only shrinks, and activating at the earliest leg of the subset can
        only leave *more* validity - so the cheapest price for a set is the cheapest price
        over all of its supersets. That is a superset-min, filled by dynamic programming
        (n * 2^n steps) instead of re-testing every plan against every set. Only the
        exact partition search needs the dense form; the contiguous-window fallback reads
        the sparse map directly, so a long trip never allocates 2^n.
        """
        inf = float("inf")
        size = 1 << n
        costs = [inf] * size
        plans: list[Any] = [None] * size
        for m, (cost, plan) in base.items():
            if cost < costs[m]:
                costs[m], plans[m] = cost, plan
        for b in range(n):
            for mask in range(size):
                if mask >> b & 1:
                    continue
                up = mask | (1 << b)
                if costs[up] < costs[mask]:
                    costs[mask], plans[mask] = costs[up], plans[up]
        return costs, plans

    def _partition_frontier(
        self, costs: list[float], plans: list[Any], bit: dict[int, int], leg_of: dict[int, int]
    ) -> dict[int, list[tuple[int, Any]]]:
        """Exact cheapest purchase split for each number of eSIMs.

        Every way of splitting the legs into k groups is considered, so a 2-eSIM answer
        can be two multi-country plans, not one plan plus singles. The lowest leg in a
        set always forms the first group, which visits each split once instead of k!
        times: 3^n states overall, which is why the caller caps n.
        """
        n = len(self.legs)
        inf = float("inf")
        size = 1 << n
        dp = [[inf] * (n + 1) for _ in range(size)]
        take = [[-1] * (n + 1) for _ in range(size)]
        dp[0][0] = 0.0
        for mask in sorted(range(1, size), key=lambda m: bin(m).count("1")):
            low = mask & -mask
            rest = mask ^ low
            sub = rest
            while True:
                group = sub | low
                cg = costs[group]
                if cg < inf:
                    prev = mask ^ group
                    for k in range(1, n + 1):
                        pc = dp[prev][k - 1]
                        if pc == inf:
                            continue
                        nc = pc + cg
                        if nc < dp[mask][k]:
                            dp[mask][k], take[mask][k] = nc, group
                if sub == 0:
                    break
                sub = (sub - 1) & rest
        out: dict[int, list[tuple[int, Any]]] = {}
        all_mask = size - 1
        for k in range(1, n + 1):
            if dp[all_mask][k] == inf:
                continue
            out[k] = self._rebuild(all_mask, k, dp, take, costs, plans)
        return out

    def _window_frontier(
        self, base: dict[int, tuple[float, Any]], bit: dict[int, int], leg_of: dict[int, int]
    ) -> dict[int, list[tuple[int, Any]]]:
        """Same frontier restricted to contiguous travel windows.

        Used above the exact ceiling: each purchase then covers one uninterrupted run of
        legs, which is almost always what a real trip buys (a regional plan for the first
        fortnight, another for the second half). Cheaper splits that interleave legs are
        not found, and the report says the restriction applied.
        """
        n = len(self.legs)
        inf = float("inf")
        order = [i for i, _ in self.legs]
        wmask: list[list[int]] = [[0] * n for _ in range(n)]
        for a in range(n):
            m = 0
            for b in range(a, n):
                m |= 1 << bit[order[b]]
                wmask[a][b] = m
        # Read the sparse map directly: a plan covers a window when its leg set contains
        # the window. n^2/2 windows x a few hundred candidate sets, so no 2^n table.
        costs: dict[int, float] = {}
        plans: dict[int, Any] = {}
        for a in range(n):
            for b in range(a, n):
                window = wmask[a][b]
                best_cost, best_plan = inf, None
                for m, (cost, plan) in base.items():
                    if (m & window) == window and cost < best_cost:
                        best_cost, best_plan = cost, plan
                costs[window], plans[window] = best_cost, best_plan
        dp = [[[inf] * (n + 1) for _ in range(n)] for _ in range(n)]
        split: list[list[list[Any]]] = [[[None] * (n + 1) for _ in range(n)] for _ in range(n)]
        for a in range(n):
            if costs[wmask[a][a]] < inf:
                dp[a][a][1] = costs[wmask[a][a]]
        for length in range(2, n + 1):
            for a in range(0, n - length + 1):
                b = a + length - 1
                if costs[wmask[a][b]] < inf:
                    dp[a][b][1] = costs[wmask[a][b]]
                for mid in range(a, b):
                    for k1 in range(1, n):
                        if dp[a][mid][k1] == inf:
                            continue
                        for k2 in range(1, n + 1 - k1):
                            if dp[mid + 1][b][k2] == inf:
                                continue
                            k = k1 + k2
                            nc = dp[a][mid][k1] + dp[mid + 1][b][k2]
                            if nc < dp[a][b][k]:
                                dp[a][b][k] = nc
                                split[a][b][k] = (mid, k1, k2)
        out: dict[int, list[tuple[int, Any]]] = {}

        def collect(a: int, b: int, k: int) -> list[tuple[int, Any]]:
            if k == 1:
                return [(wmask[a][b], plans[wmask[a][b]])]
            mid, k1, k2 = split[a][b][k]
            return collect(a, mid, k1) + collect(mid + 1, b, k2)

        for k in range(1, n + 1):
            if dp[0][n - 1][k] < inf:
                out[k] = collect(0, n - 1, k)
        return out

    @staticmethod
    def _rebuild(
        mask: int,
        k: int,
        dp: list[list[float]],
        take: list[list[int]],
        costs: list[float],
        plans: list[Any],
    ) -> list[tuple[int, Any]]:
        """Walk the DP's choices back into the groups that were bought."""
        out: list[tuple[int, Any]] = []
        while mask:
            group = take[mask][k]
            out.append((group, plans[group]))
            mask ^= group
            k -= 1
        return out

    def _strategy_for_split(
        self, trip: Trip, groups: list[tuple[int, Any]], leg_of: dict[int, int], k: int
    ) -> Strategy:
        """Turn (leg-set, plan) groups into a Strategy with the right family label."""
        n = len(self.legs)
        plans_out: list[StrategyPlan] = []
        assumptions: list[str] = []
        total = 0.0
        seen: dict[str, int] = {}
        for group, plan in groups:
            idxs = sorted(leg_of[b] for b in range(n) if group >> b & 1)
            s_legs = [self.leg_by_idx[i] for i in idxs]
            act = self._activation_day(s_legs)
            cost, comparable = plan.price_in(trip.currency, self.fx)
            assump = self._assumptions_for(plan, s_legs, act, trip)
            seen[plan.id] = seen.get(plan.id, 0) + 1
            plans_out.append(
                StrategyPlan(
                    plan=plan, legs=tuple(idxs), activation_day=act,
                    cost=cost, comparable=comparable, assumptions=assump,
                )
            )
            assumptions += assump
            total += cost
        repeats = {pid: c for pid, c in seen.items() if c > 1}
        if repeats:
            assumptions.append(
                "this strategy buys the same plan more than once ("
                + ", ".join(f"{c}x {self.plan_by_id[pid].name}" for pid, c in repeats.items())
                + ") - those are separate purchases, each with its own activation"
            )
        if n == 1:
            # A one-leg trip has nothing to combine, so the single purchase is the
            # per-leg pick - the label the report has always used for it.
            family = "local_only"
        elif k == 1:
            family = "whole_trip"
        elif k == n:
            family = "local_only"
        else:
            family = "hybrid"
        return self._make_strategy(family, plans_out, total, trip, assumptions)

    def _evaluate(
        self, trip: Trip, local_picks: dict[int, Any]
    ) -> tuple[dict[str, Strategy | None], dict[int, Strategy]]:
        """Cheapest strategy for every number of eSIMs, and the three named families.

        The families are labels over that frontier rather than three searches:
        `whole_trip` is the 1-purchase row, `local_only` the one-purchase-per-leg row,
        `hybrid` the cheapest row in between. They used to be found separately, which
        meant a 2-eSIM answer - the whole reason anyone reads the price of the
        whole-trip plan - was not something the engine could produce or price.
        """
        n = len(self.legs)
        if n == 0:
            return ({"local_only": None, "whole_trip": None, "hybrid": None}, {})
        base, bit, leg_of = self._base_cover_costs(trip, local_picks)
        if n <= _MAX_PARTITION_LEGS:
            self._partition_search = "exact"
            costs, plans = self._dense_cover(base, n)
            splits = self._partition_frontier(costs, plans, bit, leg_of)
        else:
            self._partition_search = "contiguous_windows"
            splits = self._window_frontier(base, bit, leg_of)
        per_k = {
            k: self._strategy_for_split(trip, groups, leg_of, k)
            for k, groups in splits.items()
        }
        strategies: dict[str, Strategy | None] = {
            "local_only": None,
            "whole_trip": None,
            "hybrid": None,
        }
        for strategy in per_k.values():
            cur = strategies[strategy.family]
            if cur is None or _less(strategy, cur):
                strategies[strategy.family] = strategy
        return strategies, per_k



    # ---- budget -----------------------------------------------------------
    @staticmethod
    def _budget_status(
        recommended: Strategy | None,
        trip: Trip,
        alternatives: dict[int, float | None] | None = None,
    ) -> dict[str, Any]:
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
        # "No strategy fits your budget" is only true if no purchase count fits it. A
        # purchase policy can rule out the ones that would, and saying "no strategy
        # exists" then sends the reader off to relax a budget that was never the problem.
        chosen = recommended.purchase_count if recommended else None
        fits = sorted(
            k for k, c in (alternatives or {}).items()
            if c is not None and k != chosen and c <= budget
        )
        if fits:
            alt_k = fits[0]
            msg = (
                f"The recommended strategy is outside your budget of "
                f"{_money(budget, trip.currency)}. A {alt_k}-purchase strategy at "
                f"{_money(alternatives[alt_k], trip.currency)} would fit - a purchase "
                f"policy is what ruled it out, not a lack of options."
            )
        else:
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
                act = self._activation_day(covered)
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
                hard_ok, _ = self._validity_info(plan, self._activation_day([leg]), [leg])
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

    # ---- recommendation policy -------------------------------------------
    def _choose(
        self, per_k: dict[int, Strategy], trip: Trip
    ) -> tuple[Strategy | None, dict[str, Any]]:
        """Pick the recommended strategy under the purchase policy in force.

        Default is unchanged: the cheapest strategy at any purchase count. Two opt-in
        ways to trade that away: a hard limit on how many eSIMs to buy, or
        `fewer_sims_pct`, which buys the fewest eSIMs whose total still lands within a
        tolerance of the cheapest answer - one less QR code is often worth a pound.

        Two orderings are worth stating, because both were gotchas in the first version.
        The tolerance is always measured against the *unrestricted* cheapest total, even
        when a purchase limit is also in force: a limit must not silently redefine
        "cheapest", or the report claims a tolerance the recommendation does not meet.
        And a budget outranks convenience - `--fewer-sims` says "a little more is fine",
        `--budget` says "no more than this", so if the cheaper split fits the budget and
        the convenient one does not, the cheaper split is recommended and the report says
        the convenience option was dropped for that reason.
        """
        feasible = {k: s for k, s in per_k.items() if s.comparable}
        policy: dict[str, Any] = {
            "rule": "cheapest",
            "max_purchases": self.max_purchases,
            "fewer_sims_pct": self.fewer_sims_pct,
            "min_feasible_purchases": min(feasible) if feasible else None,
            "max_purchases_satisfiable": True,
            "cheapest_purchases": None,
            "cheapest_cost": None,
            "tolerance_met": None,
            "budget_over_convenience": False,
            "convenience_purchases": None,
            "convenience_cost": None,
        }
        if not feasible:
            return None, policy
        cheapest_k = min(feasible, key=lambda k: (feasible[k].total_cost, k))
        cheapest_cost = feasible[cheapest_k].total_cost
        policy["cheapest_purchases"] = cheapest_k
        policy["cheapest_cost"] = cheapest_cost

        pool = feasible
        if self.max_purchases is not None:
            pool = {k: s for k, s in feasible.items() if k <= self.max_purchases}
            if not pool:
                policy["rule"] = "max_purchases"
                policy["max_purchases_satisfiable"] = False
                return None, policy

        if self.fewer_sims_pct is None:
            k_best = min(pool, key=lambda k: (pool[k].total_cost, k))
            if k_best != cheapest_k:
                policy["rule"] = "max_purchases"
            return feasible[k_best], policy

        allowed = [(k, s) for k, s in pool.items()
                   if s.total_cost <= cheapest_cost * (1 + self.fewer_sims_pct / 100.0)]
        if allowed:
            policy["rule"] = "fewer_purchases"
            policy["tolerance_met"] = True
            k_best = min(allowed, key=lambda kv: (kv[0], kv[1].total_cost))[0]
        else:
            # The limit made the tolerance unreachable. The limit is the hard constraint,
            # so honour it and buy the fewest it allows - but say the tolerance was missed.
            policy["rule"] = "fewest_within_limit"
            policy["tolerance_met"] = False
            k_best = min(pool)
        if (
            trip.budget is not None
            and k_best != cheapest_k
            and cheapest_cost <= trip.budget < feasible[k_best].total_cost
        ):
            policy["rule"] = "cheapest"
            policy["budget_over_convenience"] = True
            policy["convenience_purchases"] = k_best
            policy["convenience_cost"] = feasible[k_best].total_cost
            k_best = cheapest_k
        return feasible[k_best], policy

    def _frontier_rows(
        self, per_k: dict[int, Strategy], recommended: Strategy | None, trip: Trip
    ) -> list[dict[str, Any]]:
        """One row per feasible purchase count, priced against the cheapest of them."""
        if not per_k:
            return []
        best = min(s.total_cost for s in per_k.values())
        rows: list[dict[str, Any]] = []
        for k in sorted(per_k):
            s = per_k[k]
            delta = s.total_cost - best
            rows.append(
                {
                    "purchases": k,
                    "total_cost": _round(s.total_cost),
                    "buys_same_plan_twice": (
                        len({sp.plan.id for sp in s.plans}) != len(s.plans)
                    ),
                    "currency": trip.currency,
                    "family": s.family,
                    "vs_cheapest": _round(delta),
                    "vs_cheapest_pct": None if best <= 0 else round(delta / best * 100, 1),
                    "recommended": recommended is not None and s is recommended,
                    "plans": [
                        {
                            "name": sp.plan.name,
                            "provider": sp.plan.provider,
                            "price": _round(sp.cost),
                            "currency": trip.currency,
                            "legs": [self.leg_by_idx[i].country.name for i in sp.legs],
                            # The install day belongs to the strategy, not the plan: the same SKU
                            # installs on a different day depending on which legs this row gives it.
                            "activation_day": sp.activation_day,
                        }
                        for sp in s.plans
                    ],
                }
            )
        return rows

    # ---- main -------------------------------------------------------------
    def recommend(self, trip: Trip) -> Report:
        assert self.geo is not None, "call load() first"
        local_picks = {i: self._best_local_plan(leg, trip) for i, leg in self.legs}
        strategies, per_k = self._evaluate(trip, local_picks)
        # The recommendation is chosen first: the diagnostics need to know which plans
        # it bought, so that "feasible but not chosen" can be reported instead of lost.
        recommended, policy = self._choose(per_k, trip)

        diag = self._diagnostics(trip, strategies, local_picks, recommended)
        no_solution = self._no_solution_reason(trip, strategies, diag)
        if no_solution is None and recommended is None and not policy["max_purchases_satisfiable"]:
            # Feasible strategies exist; the caller's purchase limit refused them all. That
            # is still "nothing to recommend", and the report has to say why rather than
            # fall back to its generic "no feasible strategy" line.
            limit = self.max_purchases
            no_solution = (
                f"no strategy fits the limit of {limit} "
                f"{'eSIM' if limit == 1 else 'eSIMs'} - the fewest purchases found "
                f"for this trip is {policy['min_feasible_purchases']}"
            )
        budget_status = self._budget_status(
            recommended, trip, {k: s.total_cost for k, s in per_k.items()}
        )

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
            "partition_search": self._partition_search,
            "activation_lead_days": self.activate_lead_days,
            "purchase_counts_feasible": sorted(per_k),
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
        chosen_k = recommended.purchase_count if recommended is not None else None
        cheapest_k = policy["cheapest_purchases"]
        cheapest_cost = policy["cheapest_cost"]
        if chosen_k is not None and cheapest_k is not None:
            limit_word = "eSIM" if self.max_purchases == 1 else "eSIMs"
            if policy["rule"] == "fewer_purchases" and chosen_k != cheapest_k:
                notes.append(
                    f"Recommended the fewest eSIMs within {self.fewer_sims_pct:g}% of the "
                    f"cheapest total: {chosen_k} instead of {cheapest_k} "
                    f"({_money(cheapest_cost, trip.currency)})."
                )
            elif policy["rule"] == "fewest_within_limit":
                notes.append(
                    f"Nothing inside the {self.max_purchases}-{limit_word} limit stays "
                    f"within {self.fewer_sims_pct:g}% of the cheapest total "
                    f"({_money(cheapest_cost, trip.currency)} at {cheapest_k}); recommended "
                    f"the fewest the limit allows: {chosen_k} at "
                    f"{_money(recommended.total_cost, trip.currency)}."
                )
            elif self.max_purchases is not None and chosen_k != cheapest_k:
                notes.append(
                    f"Limited to at most {self.max_purchases} {limit_word}: "
                    f"{cheapest_k} would cost {_money(cheapest_cost, trip.currency)} "
                    f"instead of {_money(recommended.total_cost, trip.currency)}."
                )
            if policy["budget_over_convenience"]:
                notes.append(
                    f"Skipped the fewer-eSIMs option ({policy['convenience_purchases']} "
                    f"for {_money(policy['convenience_cost'], trip.currency)}) to stay "
                    f"within the budget of {_money(trip.budget, trip.currency)}."
                )
        if self.activate_lead_days:
            lead = self.activate_lead_days
            day_word = "day" if lead == 1 else "days"
            notes.append(
                f"Validity is counted from {lead} {day_word} before each plan's first arrival "
                f"(installed where the internet is known, not on arrival), so each purchase "
                f"needs {lead} more validity {day_word} than the road days it serves. Data need "
                f"is unchanged: the allowance still covers only the legs it serves, not the "
                f"lead-in {day_word}."
            )
        if self._partition_search != "exact":
            notes.append(
                f"With more than {_MAX_PARTITION_LEGS} legs, the purchase-count search "
                f"considered splits into contiguous travel windows only, so a cheaper "
                f"split that interleaves destinations may have been missed."
            )
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
            purchase_frontier=self._frontier_rows(per_k, recommended, trip),
            ladder_strategies=dict(per_k),
            purchase_policy=policy,
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
