"""Offline tests for the strategy-based recommendation engine (mocked esimdb API).

The engine builds three independent strategy families (local-only, whole-trip,
hybrid) from the acquired plans, ranks the feasible ones by cost in the requested
currency, and gates on the budget. Coverage is verified by a plan being listed on
every leg's destination page; validity requires a plan to last until each leg it
serves ends; regional plans must be data-feasible for the full set of legs they
cover.
"""

from __future__ import annotations

import httpx
import respx
from conftest import (
    build_plan_entry,
    build_plans_payload,
    make_provider,
    mock_fx_route,
    mock_geo_routes,
    mock_plans_route,
)

from esimdb_checker.itinerary import build_trip
from esimdb_checker.models import Country
from esimdb_checker.recommend import RecommendationEngine

ES = Country(code="ES", name="Spain", slug="spain", region="europe")
FR = Country(code="FR", name="France", slug="france", region="europe")
JP = Country(code="JP", name="Japan", slug="japan", region="asia")
A = make_provider("A", "AlphaSim", "alphasim", 300)
B = make_provider("B", "BetaNet", "betanet", 200)
J = make_provider("J", "JapanTel", "japantel", 150)


def entry(pid, provider, name, cap_mb, period, usd, **kw):
    return build_plan_entry(
        plan_id=pid,
        provider=provider,
        name=name,
        capacity_mb=cap_mb,
        period_days=period,
        prices={"USD": usd},
        **kw,
    )


def _trip(stops, daily_data_gb=1.0, currency="USD", budget=None):
    return build_trip(stops, daily_data_gb=daily_data_gb, currency=currency, budget=budget)


def _run(tmp_path, slugs_payloads, trip, **eng_kw):
    with respx.mock(assert_all_called=False) as router:
        mock_geo_routes(router)
        if trip.currency != "USD":
            mock_fx_route(router, trip.currency, 0.92)
        for slug, payload in slugs_payloads:
            mock_plans_route(router, slug, payload)
        client = httpx.Client(timeout=5)
        eng = RecommendationEngine(client, tmp_path, use_cache=False, **eng_kw)
        eng.load(trip)
        report = eng.recommend(trip)
        client.close()
    return report


def test_whole_trip_beats_local(tmp_path):
    # One Europe plan listed on both pages covers both legs; buying it once
    # (whole-trip) beats buying it twice, once per leg (local-only).
    trip = _trip([(ES, 5, 0), (FR, 5, 0)], daily_data_gb=1.0)
    eu = entry("eu-15", A, "Europe 15GB/30Days", 15000, 30, 20.0)
    sp = entry("sp-local", B, "Spain 2GB/30Days", 2000, 30, 5.0)
    fr = entry("fr-local", B, "France 2GB/30Days", 2000, 30, 5.0)
    rep = _run(tmp_path, [("spain", build_plans_payload([eu, sp])), ("france", build_plans_payload([eu, fr]))], trip)
    assert rep.recommended is not None
    assert rep.recommended.family == "whole_trip"
    assert rep.recommended.plans[0].plan.id == "eu-15"
    assert rep.recommended.plans[0].legs == (0, 1)
    assert rep.recommended.total_cost == 20.0
    # local-only still exists (2 purchases of the same plan) but costs more.
    assert rep.strategies["local_only"] is not None
    assert rep.strategies["local_only"].total_cost == 40.0
    assert "whole_trip" in rep.totals["feasible_families"]


def test_local_only_when_no_shared_plan(tmp_path):
    # No plan is listed on two destination pages -> no whole-trip/hybrid strategy.
    trip = _trip([(ES, 5, 0), (JP, 5, 0)], daily_data_gb=1.0)
    es = entry("es-local", A, "Spain 15GB/30Days", 15000, 30, 15.0)
    jp = entry("jp-local", J, "Japan 15GB/30Days", 15000, 30, 20.0)
    rep = _run(
        tmp_path,
        [("spain", build_plans_payload([es])), ("japan", build_plans_payload([jp]))],
        trip,
    )
    assert rep.recommended is not None
    assert rep.recommended.family == "local_only"
    assert len(rep.recommended.plans) == 2
    assert rep.recommended.total_cost == 35.0
    assert rep.strategies["whole_trip"] is None
    assert rep.strategies["hybrid"] is None


def test_dedup_keeps_cheapest_valid_for_scope(tmp_path):
    # Two Europe plans listed on both pages: a cheap 10-day plan (too short for the
    # 18-day trip) and a pricier 30-day plan (valid). The whole-trip strategy must
    # use the valid 30-day plan, not the cheaper plan that expires mid-trip.
    trip = _trip([(ES, 9, 0), (FR, 9, 0)], daily_data_gb=1.0)
    cheap = entry("eu-20-10", A, "Europe 20GB/10Days", 20000, 10, 10.0)
    pricey = entry("eu-25-30", A, "Europe 25GB/30Days", 25000, 30, 15.0)
    both = build_plans_payload([cheap, pricey])
    rep = _run(tmp_path, [("spain", both), ("france", both)], trip)
    wt = rep.strategies["whole_trip"]
    assert wt is not None
    assert wt.plans[0].plan.id == "eu-25-30"  # the valid plan, not the cheap 10-day one


def test_validity_excludes_short_plan(tmp_path):
    # A 5-day plan cannot serve a 10-day leg; the 15-day plan is used instead.
    trip = _trip([(ES, 10, 0)], daily_data_gb=1.0)
    short = entry("es-short", A, "Spain 15GB/5Days", 15000, 5, 10.0)
    long = entry("es-long", A, "Spain 15GB/15Days", 15000, 15, 15.0)
    rep = _run(tmp_path, [("spain", build_plans_payload([short, long]))], trip)
    assert rep.recommended is not None
    assert rep.recommended.plans[0].plan.id == "es-long"
    # The short plan is data-feasible but too short -> surfaced as a near-match.
    assert any(nm["plan"].endswith("15GB/5Days") for nm in rep.diagnostics["near_matches"])


def test_budget_gate(tmp_path):
    sp = build_plans_payload([entry("es-15", A, "Spain 15GB/30Days", 15000, 30, 20.0)])
    trip_over = _trip([(ES, 5, 0)], daily_data_gb=1.0, budget=15)
    trip_ok = _trip([(ES, 5, 0)], daily_data_gb=1.0, budget=25)
    rep_over = _run(tmp_path, [("spain", sp)], trip_over)
    rep_ok = _run(tmp_path, [("spain", sp)], trip_ok)
    # A strategy is still found ($20 plan); the $15 budget only flags it as over.
    assert rep_over.recommended is not None
    assert rep_over.budget_status["within_original"] is False
    assert rep_ok.recommended is not None
    assert rep_ok.budget_status["within_original"] is True


def test_always_throttled_plan_is_excluded(tmp_path):
    trip = _trip([(ES, 5, 0)], daily_data_gb=1.0)
    uncapped = entry("es-uncapped", A, "Spain 15GB/30Days", 15000, 30, 20.0)
    capped = entry("es-capped", A, "Spain 15GB/30Days 10Mbps", 15000, 30, 18.0, speed_limit=10000.0)
    rep = _run(tmp_path, [("spain", build_plans_payload([uncapped, capped]))], trip)
    assert rep.recommended is not None
    # The capped plan is cheaper but excluded; the uncapped one is recommended.
    assert rep.recommended.plans[0].plan.id == "es-uncapped"
    assert rep.totals["always_throttled_excluded"] == 1


def test_unlimited_plan_throttled_from_first_use_stays_excluded(tmp_path):
    """The real trap: an 'unlimited 5G' plan whose 1 Mbps limit is its whole product.

    Unlimited means there is no allowance to exhaust, so 'it only throttles after the
    cap' cannot apply - admitting it would trade a 30 GB plan for 15 days at 1 Mbps.
    """
    trip = _trip([(ES, 15, 0)], daily_data_gb=1.5, currency="USD")
    unlimited = entry("unl", A, "Fully Unlimited 5G", 0, 15, 7.16,
                      speed_limit=1000.0, possible_throttling=True)
    metered = entry("metered", A, "30GB/20Days", 30000, 20, 7.81)
    rep = _run(tmp_path, [("spain", build_plans_payload([unlimited, metered]))], trip)
    assert rep.recommended.plans[0].plan.id == "metered"
    assert rep.totals["always_throttled_excluded"] == 1


def test_provider_filter(tmp_path):
    trip = _trip([(ES, 5, 0)], daily_data_gb=1.0)
    a_plan = entry("es-a", A, "Spain 15GB/30Days (Alpha)", 15000, 30, 20.0)
    b_plan = entry("es-b", B, "Spain 15GB/30Days (Beta)", 15000, 30, 15.0)
    rep = _run(tmp_path, [("spain", build_plans_payload([a_plan, b_plan]))], trip, provider="AlphaSim")
    assert rep.recommended is not None
    # Provider filter restricts candidates to AlphaSim (the pricier one here).
    assert rep.recommended.plans[0].plan.provider == "AlphaSim"
    assert rep.recommended.plans[0].plan.id == "es-a"


# ---------------------------------------------------------------------------
# exclusion diagnostics: data need filters first; the cap is what gets reported
# ---------------------------------------------------------------------------
def test_plans_below_the_data_need_are_filtered_not_listed(tmp_path):
    """A 500 MB/day plan on a 1.5 GB/day trip was never a candidate -> no diagnostic row."""
    from esimdb_checker.report import render_console

    trip = _trip([(ES, 4, 0)], daily_data_gb=1.5, currency="USD")
    tiny = entry("tiny", A, "Spain 1 Day / Daily 500MB", 500, 1, 0.20,
                 data_cap_per="day", reduced_speed=128)
    rep = _run(tmp_path, [("spain", build_plans_payload([tiny]))], trip)
    d = rep.diagnostics
    assert d["excluded_always_throttled"] == []   # not a candidate -> never a cap row
    assert d["data_insufficient_filtered"] == 1
    text = render_console(rep)
    assert "Spain 1 Day / Daily 500MB" not in text
    assert "filtered before ranking (data below this trip's need): 1" in text


def test_after_allowance_throttle_is_a_candidate_and_is_flagged(tmp_path):
    """'3 GB/day then 128 kbps' covers a 1.5 GB/day trip in full speed - buy it.

    The throttle is unreachable when the plan already meets the need, so it is not an
    exclusion; but a plan carrying one must still be labelled in the buy list.
    """
    from esimdb_checker.report import render_console

    trip = _trip([(ES, 4, 0)], daily_data_gb=1.5, currency="USD")
    throttled = entry("throttled", A, "Spain Daily 3GB", 3000, 30, 3.0,
                      data_cap_per="day", reduced_speed=128)
    clean = entry("clean", A, "Spain Daily 4GB", 4000, 30, 9.0, data_cap_per="day")
    rep = _run(tmp_path, [("spain", build_plans_payload([throttled, clean]))], trip)
    assert rep.recommended.plans[0].plan.id == "throttled"   # cheapest, and allowed
    assert rep.diagnostics["excluded_always_throttled"] == []
    assert rep.diagnostics["after_cap_admitted"] == 1
    text = render_console(rep)
    assert "then 128 kbps after the allowance" in text        # flagged on its own row
    assert "kept as candidates: 1" in text


def test_strict_speed_cap_still_excludes_after_allowance_throttles(tmp_path):
    """--strict-speed-cap restores the old rule, and says so in the diagnostics."""
    from esimdb_checker.report import render_console

    trip = _trip([(ES, 4, 0)], daily_data_gb=1.5, currency="USD")
    throttled = entry("throttled", A, "Spain Daily 3GB", 3000, 30, 3.0,
                      data_cap_per="day", reduced_speed=128)
    clean = entry("clean", A, "Spain Daily 4GB", 4000, 30, 9.0, data_cap_per="day")
    payload = build_plans_payload([throttled, clean])
    rep = _run(tmp_path, [("spain", payload)], trip, strict_speed_cap=True)
    assert rep.recommended.plans[0].plan.id == "clean"
    assert rep.diagnostics["after_cap_excluded_strict"] == 1
    assert rep.diagnostics["after_cap_admitted"] == 0
    assert "excluded by --strict-speed-cap: 1" in render_console(rep)


def test_cap_bucket_holds_only_the_plans_that_could_have_been_bought(tmp_path):
    """A hard-capped plan below the data need is filtered silently; the viable one is listed."""
    trip = _trip([(ES, 4, 0)], daily_data_gb=1.5, currency="USD")
    short = entry("short", A, "Spain 1 Day / Daily 500MB", 500, 1, 0.20,
                  data_cap_per="day", speed_limit=1000)
    viable = entry("viable", A, "Spain Daily 3GB capped", 3000, 30, 3.0,
                   data_cap_per="day", speed_limit=1000)
    rep = _run(tmp_path, [("spain", build_plans_payload([short, viable]))], trip)
    d = rep.diagnostics
    assert [e["name"] for e in d["excluded_always_throttled"]] == ["Spain Daily 3GB capped"]
    assert d["data_insufficient_filtered"] == 1
    assert d["excluded_always_throttled_total"] == 1


def test_pooled_plan_too_small_for_every_leg_it_covers(tmp_path):
    """A 200MB total plan covering a 4-day leg (6 GB needed) is filtered, not listed."""
    trip = _trip([(ES, 4, 0)], daily_data_gb=1.5, currency="USD")
    pooled = entry("pool", A, "Spain 1 Day / Total 200MB", 200, 1, 0.10,
                   reduced_speed=128)
    rep = _run(tmp_path, [("spain", build_plans_payload([pooled]))], trip)
    assert rep.diagnostics["excluded_always_throttled"] == []
    assert rep.diagnostics["data_insufficient_filtered"] == 1


def test_exclusion_counts_are_totals_not_list_lengths(tmp_path):
    """A capped detail list must never be read as 'there were exactly N'."""
    from esimdb_checker.report import render_console

    trip = _trip([(ES, 4, 0)], daily_data_gb=1.5, currency="USD")
    plans = [entry(f"c{i}", A, f"Spain Daily 3GB v{i}", 3000, 30, 3.0,
                   data_cap_per="day", speed_limit=1000) for i in range(25)]
    rep = _run(tmp_path, [("spain", build_plans_payload(plans))], trip)
    d = rep.diagnostics
    assert d["excluded_always_throttled_total"] == 25
    assert len(d["excluded_always_throttled"]) < 25
    assert rep.totals["always_throttled_excluded"] == 25    # not the 20-row list length
    assert "… 5 more (20 shown)" in render_console(rep)


def test_plan_names_are_unescaped_once_at_parse(tmp_path):
    """'Asia &amp; Europe' from the payload must not reach a report as '&amp;'."""
    from esimdb_checker.report import render_console

    trip = _trip([(ES, 4, 0)], daily_data_gb=1.5, currency="USD")
    amp = entry("amp", A, "Asia &amp; Europe 52 Countries - Daily 3GB", 3000, 30, 3.0,
                data_cap_per="day", speed_limit=1000)
    rep = _run(tmp_path, [("spain", build_plans_payload([amp]))], trip)
    names = [e["name"] for e in rep.diagnostics["excluded_always_throttled"]]
    assert names == ["Asia & Europe 52 Countries - Daily 3GB"]
    assert "&amp;" not in render_console(rep)
