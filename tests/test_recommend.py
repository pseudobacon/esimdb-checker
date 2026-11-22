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
import pytest
import respx
from conftest import (
    build_plan_entry,
    build_plans_payload,
    make_provider,
    mock_fx_route,
    mock_geo_routes,
    mock_plans_route,
)

from esimdb_checker.itinerary import StopError, build_trip
from esimdb_checker.models import Country
from esimdb_checker.recommend import _MAX_PARTITION_LEGS, RecommendationEngine
from esimdb_checker.report import render_console

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


def _trip(stops, daily_data_gb=1.0, currency="USD", budget=None, transfer_data=0.0):
    return build_trip(stops, daily_data_gb=daily_data_gb, currency=currency, budget=budget,
                      transfer_data=transfer_data)


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
    rep = _run(
        tmp_path,
        [("spain", build_plans_payload([eu, sp])), ("france", build_plans_payload([eu, fr]))],
        trip,
    )
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
    rep = _run(
        tmp_path, [("spain", build_plans_payload([a_plan, b_plan]))], trip, provider="AlphaSim"
    )
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
    text = render_console(rep, verbose=True)
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
    text = render_console(rep, verbose=True)
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
    assert "excluded by --strict-speed-cap: 1" in render_console(rep, verbose=True)


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
    assert "… 5 more (20 shown)" in render_console(rep, verbose=True)


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


def test_regional_plan_may_serve_only_a_window_of_a_longer_trip(tmp_path):
    """A 30-day plan on a 32-day trip: not whole-trip, but it serves days 1-24.

    The page-presence pre-check used to delete such a plan before the subset pass
    could see the window it does cover, so it appeared in no strategy and in no
    bucket.
    """
    trip = _trip([(ES, 12, 0), (FR, 12, 0), (JP, 8, 0)], daily_data_gb=1.0)
    shared = entry("eu-asia-50-30", A, "Eurasia 50GB/30Days", 50000, 30, 18.0)
    es = entry("es-13-30", B, "Spain 13GB/30Days", 13000, 30, 12.0)
    fr = entry("fr-13-30", B, "France 13GB/30Days", 13000, 30, 12.0)
    jp = entry("jp-9-30", J, "Japan 9GB/30Days", 9000, 30, 8.0)
    rep = _run(
        tmp_path,
        [("spain", build_plans_payload([shared, es])),
         ("france", build_plans_payload([shared, fr])),
         ("japan", build_plans_payload([shared, jp]))],
        trip,
    )
    assert rep.trip.span_days == 32
    # The listed period ends on day 30, so no single plan can cover all 32 days.
    assert rep.strategies["whole_trip"] is None
    hy = rep.strategies["hybrid"]
    assert hy is not None
    window = [sp for sp in hy.plans if sp.plan.id == "eu-asia-50-30"]
    assert window, "the 30-day plan should serve the first two legs"
    assert window[0].legs == (0, 1)
    assert rep.recommended is not None
    assert rep.recommended.family == "hybrid"
    assert rep.recommended.total_cost == 26.0  # shared 18 + a Japan plan 8
    # Why it is not a whole-trip purchase is stated, not left to inference.
    named = {e["id"]: e for e in rep.diagnostics["regional_near_matches"]}
    assert "eu-asia-50-30" in named
    # Counted from the install day, which is the day before the first arrival by default:
    # installed on travel day 0, 30 days of validity reaches day 29, and the trip ends on 32.
    assert named["eu-asia-50-30"]["short_by_days"] == 3


def test_one_plan_can_cover_two_visits_to_the_same_country(tmp_path):
    """Repeat visits used to force two purchases even when one plan spans both."""
    trip = _trip([(ES, 5, 0), (FR, 3, 0), (ES, 5, 0)], daily_data_gb=1.0)
    es_long = entry("es-12-30", A, "Spain 12GB/30Days", 12000, 30, 6.0)
    fr = entry("fr-5-30", B, "France 5GB/30Days", 5000, 30, 4.0)
    rep = _run(
        tmp_path,
        [("spain", build_plans_payload([es_long])), ("france", build_plans_payload([fr]))],
        trip,
    )
    hy = rep.strategies["hybrid"]
    assert hy is not None and hy.purchase_count == 2
    shared = [sp for sp in hy.plans if sp.plan.id == "es-12-30"]
    assert shared and shared[0].legs == (0, 2)
    assert rep.recommended is not None and rep.recommended.total_cost == 10.0
    assert rep.strategies["local_only"].total_cost == 16.0  # the same plan bought twice
    # Buying two copies of one SKU is legal, so the ladder keeps that row - but it says
    # so, rather than leaving a reader to notice the same eSIM listed twice.
    rows = _rows(rep)
    assert rows[3]["buys_same_plan_twice"] is True
    assert rows[2]["buys_same_plan_twice"] is False


def test_calendar_only_multi_country_misses_are_named(tmp_path):
    """Enough data and the right pages, too short for the calendar: named."""
    from esimdb_checker.report import render_console

    trip = _trip([(ES, 9, 0), (FR, 9, 0)], daily_data_gb=1.0)
    short = entry("eu-20-3", A, "Europe 20GB/3Days", 20000, 3, 5.0)
    fits = entry("eu-25-30", A, "Europe 25GB/30Days", 25000, 30, 15.0)
    both = build_plans_payload([short, fits])
    rep = _run(tmp_path, [("spain", both), ("france", both)], trip)
    named = {e["id"] for e in rep.diagnostics["regional_near_matches"]}
    assert "eu-20-3" in named
    assert "eu-25-30" not in named  # it serves the trip, so it is no kind of miss
    assert rep.diagnostics["regional_alternatives_total"] == 0
    assert "not enough calendar" in render_console(rep, verbose=True)


def test_feasible_multi_leg_plans_that_werent_bought_are_still_listed(tmp_path):
    """The strategy table shows one winner per family; other workable plans go unseen."""
    trip = _trip([(ES, 5, 0), (FR, 5, 0), (JP, 5, 0)], daily_data_gb=1.0)
    x = entry("eurasia-30-60", A, "Eurasia 30GB/60Days", 30000, 60, 10.0)
    y = entry("eu-20-30", B, "Europe 20GB/30Days", 20000, 30, 12.0)
    es = entry("es-5-10", J, "Spain 5GB/10Days", 5000, 10, 7.0)
    fr = entry("fr-5-10", J, "France 5GB/10Days", 5000, 10, 7.0)
    jp = entry("jp-5-10", J, "Japan 5GB/10Days", 5000, 10, 7.0)
    rep = _run(
        tmp_path,
        [("spain", build_plans_payload([x, y, es])),
         ("france", build_plans_payload([x, y, fr])),
         ("japan", build_plans_payload([x, jp]))],
        trip,
    )
    assert rep.recommended is not None and rep.recommended.total_cost == 10.0
    alts = rep.diagnostics["regional_alternatives"]
    assert any(e["id"] == "eu-20-30" and e["legs"] == ["Spain", "France"]
               and e["covers_days"] == "1-10" for e in alts)
    assert all(e["id"] != "eurasia-30-60" for e in alts)  # it was bought
    assert rep.diagnostics["regional_alternatives_total"] == 1


def test_transfer_days_are_included_in_the_reported_travel_day_span(tmp_path):
    """A transit day is uncharged but real: the report said 1-12 for a 13-day span."""
    from esimdb_checker.report import render_console

    trip = _trip([(ES, 5, 1), (FR, 7, 0)], daily_data_gb=1.0)
    es = entry("es-5-30", A, "Spain 5GB/30Days", 5000, 30, 5.0)
    fr = entry("fr-7-30", B, "France 7GB/30Days", 7000, 30, 6.0)
    rep = _run(
        tmp_path,
        [("spain", build_plans_payload([es])), ("france", build_plans_payload([fr]))],
        trip,
    )
    assert rep.trip.total_days == 12 and rep.trip.span_days == 13
    text = render_console(rep)
    assert "12 charged day(s) over 13 travel day(s)" in text
    assert "1–13" in text


# ---------------------------------------------------------------------------
# --transfer-data: flying days charge a fraction of the daily need
# ---------------------------------------------------------------------------
def test_transfer_data_charges_flying_days_into_the_requirements():
    stops = [(ES, 4, 1), (FR, 5, 0), (None, 0, 1)]   # pre-day on ES, tail layover after FR
    plain = build_trip(stops, daily_data_gb=1.0, currency="USD")
    assert [leg.required_gb for leg in plain.legs] == [4.0, 5.0, 0.0]
    assert plain.required_gb == 9.0

    half = build_trip(stops, daily_data_gb=1.0, currency="USD", transfer_data=0.5)
    # The pre-day rides on its own leg; the tail 0-day leg's share rides on the previous leg.
    assert [leg.required_gb for leg in half.legs] == [4.5, 5.5, 0.0]
    assert half.required_gb == 10.0
    assert half.transfer_data == 0.5

    with pytest.raises(StopError):
        build_trip(stops, daily_data_gb=1.0, currency="USD", transfer_data=1.5)


def test_a_plan_ending_before_a_charged_tail_day_is_infeasible(tmp_path):
    """With a charged layover day, the covering plan must stay valid through it."""
    stops = [(ES, 4, 0), (None, 0, 1)]
    short = entry("es-5d", A, "Spain 5GB/5Days", 5000, 5, 4.0)
    exact = entry("es-6d", A, "Spain 6GB/6Days", 6000, 6, 6.0)
    longp = entry("es-30d", A, "Spain 10GB/30Days", 10000, 30, 9.0)
    payload = [("spain", build_plans_payload([short, exact, longp]))]
    # F=0: the layover needs nothing, the short 5-day plan (activated day 0, ends day 4) wins.
    rep0 = _run(tmp_path, payload, _trip(stops, daily_data_gb=1.0))
    assert rep0.recommended.plans[0].plan.id == "es-5d"
    # F=0.5: day 5 needs 0.5 GB from a valid plan. The 5-day plan (ends day 4) dies on
    # validity; the 6-day plan ends exactly on the layover day and must stay feasible -
    # this pins the slot arithmetic (a plan valid through day 5 is not short by one day).
    rep1 = _run(tmp_path, payload, _trip(stops, daily_data_gb=1.0, transfer_data=0.5))
    assert rep1.recommended.plans[0].plan.id == "es-6d"


def test_transfer_data_charges_the_pool_not_just_the_header(tmp_path):
    """The half-day must come out of the serving plan's allowance."""
    stops = [(ES, 4, 1)]   # 4 GB + 0.5 GB flying day = 4.5 GB needed
    tight = entry("es-tight", A, "Spain 4.5GB/30Days", 4500, 30, 5.0)   # 4.39 GB: short by a hair
    roomy = entry("es-roomy", A, "Spain 10GB/30Days", 10000, 30, 9.0)
    payload = [("spain", build_plans_payload([tight, roomy]))]
    rep0 = _run(tmp_path, payload, _trip(stops, daily_data_gb=1.0))
    assert rep0.recommended.plans[0].plan.id == "es-tight"     # 4 GB fits 4.39 GB
    rep1 = _run(tmp_path, payload, _trip(stops, daily_data_gb=1.0, transfer_data=0.5))
    assert rep1.recommended.plans[0].plan.id == "es-roomy"     # 4.5 GB does not


def test_pre_day_needs_the_plan_already_active(tmp_path):
    """Installing on arrival (lead 0) leaves a charged flying day without an active plan."""
    stops = [(ES, 4, 1)]
    only = entry("es-only", A, "Spain 10GB/30Days", 10000, 30, 9.0)
    payload = [("spain", build_plans_payload([only]))]
    # Default lead 1: activated day 1, active on the flying day (day 1) - feasible.
    ok = _run(tmp_path, payload, _trip(stops, daily_data_gb=1.0, transfer_data=0.5))
    assert ok.recommended is not None
    # Lead 0: activated day 2, the flying day (day 1) has no active plan - infeasible.
    bad = _run(tmp_path, payload, _trip(stops, daily_data_gb=1.0, transfer_data=0.5),
               activate_lead_days=0)
    assert bad.recommended is None
    assert all(s is None for s in bad.strategies.values())


def test_wide_trips_search_windows_and_say_so(tmp_path):
    """Above the subset ceiling the search narrows - and the report admits it."""
    from esimdb_checker.report import render_console

    stops = [(ES, 3, 0) if i % 2 == 0 else (FR, 3, 0) for i in range(10)]
    trip = _trip(stops, daily_data_gb=1.0)
    both = build_plans_payload([
        entry("eu-60-40", A, "Europe 60GB/40Days", 60000, 40, 30.0),
        entry("es-3-20", B, "Spain 3GB/20Days", 3000, 20, 3.0),
        entry("fr-3-20", J, "France 3GB/20Days", 3000, 20, 3.0),
    ])
    rep = _run(tmp_path, [("spain", both), ("france", both)], trip)
    assert len(rep.trip.legs) == 10
    assert rep.totals["regional_scope_search"] == "contiguous_windows_for_wide_plans"
    assert "contiguous travel windows" in render_console(rep, verbose=True)
    # Contiguous windows still find the pair that a whole-set check would have rejected.
    hy = rep.strategies["hybrid"]
    assert hy is not None
    assert any(sp.plan.id == "eu-60-40" and len(sp.legs) >= 2 for sp in hy.plans)


# ---------------------------------------------------------------------------
# Cost of convenience: the cheapest strategy for each number of eSIMs.
#
# The families used to be three separate searches, which meant the engine could
# price "one plan for everything" and "one plan per destination" but had no way to
# say what two plans would cost - the question people actually ask when a whole-trip
# plan looks expensive. The frontier prices every purchase count, and the two
# policies below act on it.


TH = Country(code="TH", name="Thailand", slug="thailand", region="asia")


def _frontier_payloads():
    """One world plan (3 legs), a Europe plan (2 legs), and a local pick per leg."""
    glob = entry("glob-60", A, "World 60GB/60Days", 60000, 60, 30.0)
    reg = entry("eu-15", B, "Europe 15GB/30Days", 15000, 30, 11.0)
    es = entry("es-6", B, "Spain 6GB/30Days", 6000, 30, 5.0)
    fr = entry("fr-6", B, "France 6GB/30Days", 6000, 30, 5.0)
    jp = entry("jp-6", J, "Japan 6GB/30Days", 6000, 30, 5.0)
    return [
        ("spain", build_plans_payload([glob, reg, es])),
        ("france", build_plans_payload([glob, reg, fr])),
        ("japan", build_plans_payload([glob, jp])),
    ]


def _rows(rep):
    return {r["purchases"]: r for r in rep.purchase_frontier}


def test_frontier_prices_every_number_of_esims(tmp_path):
    trip = _trip([(ES, 5, 0), (FR, 5, 0), (JP, 5, 0)])
    rep = _run(tmp_path, _frontier_payloads(), trip)
    rows = _rows(rep)
    assert sorted(rows) == [1, 2, 3]
    assert rows[1]["total_cost"] == pytest.approx(30.0)
    assert rows[1]["family"] == "whole_trip"
    assert rows[2]["total_cost"] == pytest.approx(16.0)
    assert rows[2]["family"] == "hybrid"
    assert len(rows[2]["plans"]) == 2
    assert rows[3]["total_cost"] == pytest.approx(15.0)
    assert rows[3]["family"] == "local_only"
    # Priced against the cheapest rung, and the cheapest rung is what gets recommended.
    assert rows[3]["vs_cheapest_pct"] == 0.0
    assert rows[2]["vs_cheapest_pct"] == pytest.approx(6.7)
    assert rows[3]["recommended"] is True
    assert rows[1]["recommended"] is False
    # The ladder renders as the boxed table with a what-it-buys column.
    text = render_console(rep)
    assert "What it buys" in text and "│ eSIMs │" in text
    assert "▸" in text  # the recommended rung is marked


def test_show_purchases_expands_a_row_without_changing_the_answer(tmp_path):
    """The ladder priced six options but said what only one of them installs."""
    trip = _trip([(ES, 5, 0), (FR, 5, 0), (JP, 5, 0)])
    rep = _run(tmp_path, _frontier_payloads(), trip)
    before = render_console(rep)

    rep.totals["show_purchases"] = [2]
    after = render_console(rep)
    head = next(ln for ln in after.splitlines() if ln.startswith("  What 2 eSIM"))
    assert "+6.7% vs cheapest" in head
    block = after.split(head, 1)[1].split("RECOMMENDED", 1)[0]
    # The row is described in the same detail as the recommendation, not as a name list.
    assert block.count("buy ") == 2
    assert "Europe 15GB/30Days" in block and "Japan 6GB/30Days" in block
    assert "travel day" in block
    # Reporting only: the recommendation and its own buy list are untouched.
    assert "RECOMMENDED" in after and "buy 3." in after.split("RECOMMENDED", 1)[1]
    prefix = "  buy "
    assert (sum(ln.startswith(prefix) for ln in before.splitlines()) + 2
            == sum(ln.startswith(prefix) for ln in after.splitlines()))


def test_show_purchases_refuses_a_count_that_no_strategy_buys(tmp_path):
    trip = _trip([(ES, 5, 0), (FR, 5, 0), (JP, 5, 0)])
    rep = _run(tmp_path, _frontier_payloads(), trip)
    rep.totals["show_purchases"] = [9]
    assert "Nothing feasible buys exactly 9 eSIMs" in render_console(rep)


def test_show_purchases_markdown_expands_the_same_rows(tmp_path):
    from esimdb_checker.report import render_markdown
    trip = _trip([(ES, 5, 0), (FR, 5, 0), (JP, 5, 0)])
    rep = _run(tmp_path, _frontier_payloads(), trip)
    rep.totals["show_purchases"] = [1, 2]
    md = render_markdown(rep)
    assert "**What 1 eSIM would buy**" in md and "**What 2 eSIMs would buy**" in md
    bullets = md.split("**What 1 eSIM would buy**", 1)[1].split("**What 2 eSIMs")[0]
    assert bullets.count("\n- **") == 1
    # The human report no longer carries the audit trail; the assumptions stay in the data.
    assert "Verification assumptions:" not in md
    assert rep.ladder_strategies[1].assumptions


def test_expanded_row_assumptions_stay_in_the_data_not_the_output(tmp_path):
    """The audit trail (activation assumptions) stays in the JSON; the output skips it."""
    trip = _trip([(ES, 5, 0), (FR, 5, 0), (JP, 5, 0)])
    rep = _run(tmp_path, _frontier_payloads(), trip)
    rep.totals["show_purchases"] = [2]
    text = render_console(rep)
    block = text.split("  What 2 eSIMs would buy", 1)[1].split("RECOMMENDED", 1)[0]
    assert "verification assumptions:" not in block
    assert "activated on travel day 10" not in text
    s = rep.ladder_strategies[2]
    assert any("activated on travel day 10" in a for a in s.assumptions)


def test_expanded_row_discloses_an_accepted_speed_cap(tmp_path):
    """A capped plan inside an expanded row keeps its speed warning on the price line."""
    trip = _trip([(ES, 4, 0), (FR, 4, 0)], daily_data_gb=1.5)
    capped = entry("eu-cap", A, "Europe Daily 3GB", 3000, 30, 4.0,
                   data_cap_per="day", reduced_speed=128)
    es = entry("es-daily", B, "Spain Daily 3GB", 3000, 30, 4.0, data_cap_per="day")
    fr = entry("fr-daily", B, "France Daily 3GB", 3000, 30, 4.0, data_cap_per="day")
    rep = _run(
        tmp_path,
        [("spain", build_plans_payload([capped, es])),
         ("france", build_plans_payload([capped, fr]))],
        trip,
    )
    rep.totals["show_purchases"] = [1]
    text = render_console(rep)
    block = text.split("  What 1 eSIM would buy", 1)[1].split("RECOMMENDED", 1)[0]
    assert "128 kbps after the allowance" in block


def test_frontier_endpoints_are_the_named_families(tmp_path):
    trip = _trip([(ES, 5, 0), (FR, 5, 0), (JP, 5, 0)])
    rep = _run(tmp_path, _frontier_payloads(), trip)
    rows = _rows(rep)
    assert rows[1]["total_cost"] == pytest.approx(rep.strategies["whole_trip"].total_cost)
    assert rows[3]["total_cost"] == pytest.approx(rep.strategies["local_only"].total_cost)
    assert rows[2]["total_cost"] == pytest.approx(rep.strategies["hybrid"].total_cost)


def test_two_multi_country_plans_form_one_purchase_count(tmp_path):
    # No plan covers all four legs, and the cheap answer is *two* regional plans - a
    # shape the old single-regional-plan hybrid could not build at any price.
    trip = _trip([(ES, 5, 0), (FR, 5, 0), (JP, 5, 0), (TH, 5, 0)])
    eu = entry("eu-pair", A, "Europe pair 20GB/20Days", 20000, 20, 8.0)
    asia = entry("as-pair", J, "Asia pair 20GB/20Days", 20000, 20, 8.0)
    singles = {
        "spain": entry("es-2", B, "Spain 6GB/30Days", 6000, 30, 2.0),
        "france": entry("fr-2", B, "France 6GB/30Days", 6000, 30, 2.0),
        "japan": entry("jp-2", B, "Japan 6GB/30Days", 6000, 30, 2.0),
        "thailand": entry("th-2", B, "Thailand 6GB/30Days", 6000, 30, 2.0),
    }
    payloads = [
        ("spain", build_plans_payload([eu, singles["spain"]])),
        ("france", build_plans_payload([eu, singles["france"]])),
        ("japan", build_plans_payload([asia, singles["japan"]])),
        ("thailand", build_plans_payload([asia, singles["thailand"]])),
    ]
    rep = _run(tmp_path, payloads, trip)
    rows = _rows(rep)
    assert 1 not in rows
    assert rep.strategies["whole_trip"] is None
    assert rows[2]["total_cost"] == pytest.approx(16.0)
    assert sorted(len(p["legs"]) for p in rows[2]["plans"]) == [2, 2]
    # Four single-destination plans are still cheaper, and stay the default.
    assert rows[4]["total_cost"] == pytest.approx(8.0)
    assert rep.recommended.purchase_count == 4

    limited = _run(tmp_path, payloads, trip, max_purchases=2)
    assert limited.recommended.purchase_count == 2
    assert limited.recommended.total_cost == pytest.approx(16.0)
    assert "at most 2 eSIMs" in " ".join(limited.notes)
    assert "What it buys" in render_console(limited)


def test_purchase_limit_that_nothing_fits_recommends_nothing(tmp_path):
    trip = _trip([(ES, 5, 0), (FR, 5, 0), (JP, 5, 0), (TH, 5, 0)])
    eu = entry("eu-pair", A, "Europe pair 20GB/20Days", 20000, 20, 8.0)
    asia = entry("as-pair", J, "Asia pair 20GB/20Days", 20000, 20, 8.0)
    payloads = [
        ("spain", build_plans_payload([eu, entry("es-2", B, "Spain 6GB", 6000, 30, 2.0)])),
        ("france", build_plans_payload([eu, entry("fr-2", B, "France 6GB", 6000, 30, 2.0)])),
        ("japan", build_plans_payload([asia, entry("jp-2", B, "Japan 6GB", 6000, 30, 2.0)])),
        ("thailand", build_plans_payload([asia, entry("th-2", B, "Thailand 6GB", 6000, 30, 2.0)])),
    ]
    rep = _run(tmp_path, payloads, trip, max_purchases=1)
    assert rep.recommended is None
    # Strategies exist; the limit refused them. The report must say so, not just go quiet.
    # ("found", not "possible": above the ceiling the split search is restricted.)
    assert "fewest purchases found" in " ".join(rep.notes)
    assert rep.purchase_policy["min_feasible_purchases"] == 2
    assert "fewest purchases found" in render_console(rep, verbose=True)


def test_fewer_sims_buys_convenience_within_a_tolerance(tmp_path):
    trip = _trip([(ES, 5, 0), (FR, 5, 0), (JP, 5, 0)])
    payloads = _frontier_payloads()
    # 2 eSIMs costs 6.7% more than 3, so a 10% tolerance takes it...
    loose = _run(tmp_path, payloads, trip, fewer_sims_pct=10)
    assert loose.recommended.purchase_count == 2
    assert loose.recommended.total_cost == pytest.approx(16.0)
    assert loose.purchase_policy["rule"] == "fewer_purchases"
    assert "fewest eSIMs within 10%" in " ".join(loose.notes)
    assert "fewest eSIMs within 10% of the cheapest total" in render_console(loose)
    # ...and a 5% tolerance does not. One eSIM is 100% more, so it never wins here.
    tight = _run(tmp_path, payloads, trip, fewer_sims_pct=5)
    assert tight.recommended.purchase_count == 3


def test_frontier_falls_back_to_contiguous_travel_windows(tmp_path):
    n = _MAX_PARTITION_LEGS + 1
    countries = [
        Country(code=f"C{i}", name=f"Country{i}", slug=f"country{i}", region="asia")
        for i in range(n)
    ]
    trip = _trip([(c, 2, 0) for c in countries])
    glob = entry("glob", A, "World 60GB/60Days", 500000, 400, 50.0)
    payloads = [
        (c.slug, build_plans_payload([glob, entry(f"l{i}", B, f"{c.name} 6GB", 6000, 30, 2.0)]))
        for i, c in enumerate(countries)
    ]
    rep = _run(tmp_path, payloads, trip)
    assert len(rep.trip.legs) == n
    assert rep.totals["partition_search"] == "contiguous_windows"
    assert "contiguous travel windows" in " ".join(rep.notes)
    rows = _rows(rep)
    # The one-plan answer is a contiguous window trivially, so it survives the fallback.
    assert rows[1]["total_cost"] == pytest.approx(50.0)
    assert rows[n]["total_cost"] == pytest.approx(2.0 * n)


def test_a_limit_does_not_redefine_what_the_tolerance_is_measured_against(tmp_path):
    # `--fewer-sims` promises "within PCT% of the cheapest". Measuring that against the
    # cheapest *inside the limit* lets --max-purchases 1 claim a 10% tolerance while
    # recommending 44% more. The floor is always the unrestricted cheapest.
    trip = _trip([(ES, 5, 0), (FR, 5, 0), (JP, 5, 0)])
    rep = _run(tmp_path, _frontier_payloads(), trip, fewer_sims_pct=10, max_purchases=1)
    assert rep.recommended.purchase_count == 1
    assert rep.purchase_policy["rule"] == "fewest_within_limit"
    assert rep.purchase_policy["tolerance_met"] is False
    assert rep.purchase_policy["cheapest_cost"] == pytest.approx(15.0)
    text = render_console(rep)
    assert "fewest eSIMs the 1-eSIM limit allows" in text
    assert "Nothing inside the 1-eSIM limit stays within 10%" in " ".join(rep.notes)
    assert "fewest eSIMs within 10% of the cheapest total" not in text


def test_tolerance_choosing_the_cheapest_split_prints_no_vacuous_note(tmp_path):
    # Whole-trip is both the cheapest and the fewest here, so the old code printed
    # "the fewest eSIMs ...: 1 instead of 1".
    trip = _trip([(ES, 5, 0), (FR, 5, 0)])
    glob = entry("glob-20", A, "World 20GB/30Days", 20000, 30, 8.0)
    es = entry("es-6", B, "Spain 6GB/30Days", 6000, 30, 5.0)
    fr = entry("fr-6", B, "France 6GB/30Days", 6000, 30, 5.0)
    payloads = [
        ("spain", build_plans_payload([glob, es])),
        ("france", build_plans_payload([glob, fr])),
    ]
    rep = _run(tmp_path, payloads, trip, fewer_sims_pct=30)
    assert rep.recommended.purchase_count == 1
    joined = " ".join(rep.notes)
    assert "instead of 1" not in joined
    assert "fewest eSIMs within 30%" not in joined


def test_a_budget_outranks_the_convenience_option(tmp_path):
    # --fewer-sims says "a little more is fine"; --budget says "no more than this".
    # When the convenient split busts the budget and the cheap one does not, the cheap
    # one is recommended - and the budget line stays the truth.
    payloads = _frontier_payloads()
    rep = _run(tmp_path, payloads, _trip([(ES, 5, 0), (FR, 5, 0), (JP, 5, 0)], budget=15.5),
               fewer_sims_pct=10)
    assert rep.recommended.purchase_count == 3  # the 2-eSIM split costs 16.00
    assert rep.purchase_policy["budget_over_convenience"] is True
    assert rep.budget_status["within_original"] is True
    assert "Skipped the fewer-eSIMs option" in " ".join(rep.notes)

    # A hard limit is not negotiable, so it can recommend over budget - but the budget
    # line must say which purchase count would have fitted, not "nothing exists".
    limited = _run(tmp_path, payloads, _trip([(ES, 5, 0), (FR, 5, 0), (JP, 5, 0)], budget=15.5),
                   max_purchases=2)
    assert limited.recommended.purchase_count == 2
    msg = limited.budget_status["message"]
    assert "3-purchase strategy" in msg and "ruled it out" in msg

    # And when genuinely nothing fits, the original wording stays.
    broke = _run(tmp_path, payloads, _trip([(ES, 5, 0), (FR, 5, 0), (JP, 5, 0)], budget=14.0))
    assert "would fit" not in broke.budget_status["message"]
    assert "No eSIM strategy meeting your data requirement" in broke.budget_status["message"]


def test_activating_before_arrival_buys_fewer_esims(tmp_path):
    """The lead-in day costs *validity*, not data - and it falls hardest on the cheap
    short-validity plans that make many-eSIM splits cheap, so the optimum moves the
    other way from intuition: fewer purchases."""
    # Spain 5 days, Japan 3, Spain 5 (days 1-5, 7-9, 11-15)
    trip = _trip([(ES, 5, 0), (JP, 3, 2), (ES, 5, 2)])
    es_tight = entry("es-5-5", A, "Spain 10GB/5Days", 10000, 5, 3.0)     # exactly one visit
    es_long = entry("es-20-30", A, "Spain 20GB/30Days", 20000, 30, 7.0)  # spans both visits
    jp = entry("jp-5", B, "Japan 5GB/5Days", 5000, 5, 4.0)
    payloads = [("spain", build_plans_payload([es_tight, es_long])),
                ("japan", build_plans_payload([jp]))]

    on_arrival = _run(tmp_path, payloads, trip, activate_lead_days=0)
    assert on_arrival.recommended.purchase_count == 3          # 3 + 3 + 4
    assert on_arrival.recommended.total_cost == pytest.approx(10.0)
    assert {r["purchases"] for r in on_arrival.purchase_frontier} == {2, 3}

    day_early = _run(tmp_path, payloads, trip, activate_lead_days=1)
    # The 5-day plan now has to cover 6 days of validity, so it cannot serve a visit at
    # all; the 30-day plan covers both Spain visits in one purchase.
    assert day_early.recommended.purchase_count == 2
    assert day_early.recommended.total_cost == pytest.approx(11.0)
    rows = {r["purchases"]: r for r in day_early.purchase_frontier}
    assert set(rows) == {2, 3}
    assert rows[2]["total_cost"] == pytest.approx(11.0)
    # Three purchases is only reachable by buying the one still-valid Spain plan twice.
    assert rows[3]["buys_same_plan_twice"] is True

    # The reason is stated, not implied - and it is validity, never the data need.
    text = " ".join(day_early.notes)
    assert "1 day before each plan's first arrival" in text
    assert "Data need" in text
    for plan in day_early.recommended.plans:
        assert plan.activation_day == min(
            day_early.trip.legs[i].start_day for i in plan.legs
        ) - 1
    assert "1 day before arrival" in " ".join(day_early.recommended.plans[0].assumptions)
    assert on_arrival.diagnostics["data_insufficient_filtered"] == \
        day_early.diagnostics["data_insufficient_filtered"]


def test_activation_lead_days_defaults_to_installing_the_day_before(tmp_path):
    """The default is a product decision, so it gets asserted - it must not drift."""
    trip = _trip([(ES, 5, 0)])
    es = entry("es-10-30", A, "Spain 10GB/30Days", 10000, 30, 5.0)
    payloads = [("spain", build_plans_payload([es]))]

    default = _run(tmp_path, payloads, trip)
    assert default.totals["activation_lead_days"] == 1
    console = render_console(default)
    line = next(ln for ln in console.splitlines() if ln.startswith("Itinerary:"))
    assert "activation: 1 day before the first arrival each plan serves" in line
    assert default.recommended.plans[0].activation_day == 0
    # Travel day 0 means "before the trip starts" - the report has to say that rather than
    # point at a day the itinerary does not have.
    assert "day before the trip starts" in console

    # 0 is still reachable, and it is the only way to reproduce pre-2026-11 numbers.
    arrival = _run(tmp_path, payloads, trip, activate_lead_days=0)
    assert arrival.totals["activation_lead_days"] == 0
    assert "activation:" not in render_console(arrival)
    assert arrival.recommended.plans[0].activation_day == 1
    assert "day before the trip starts" not in render_console(arrival)

    two = _run(tmp_path, payloads, trip, activate_lead_days=2)
    assert two.recommended.plans[0].activation_day == -1
    assert "activation: 2 days before the first arrival each plan serves" in render_console(two)
