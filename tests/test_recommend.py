"""Offline tests for the recommendation engine (mocked esimdb API)."""

from __future__ import annotations

import httpx
import pytest
import respx

from conftest import (
    build_plan_entry,
    build_plans_payload,
    make_provider,
    mock_geo_routes,
    mock_plans_route,
)
from esimdb_checker.models import Country, Itinerary
from esimdb_checker.recommend import RecommendationEngine

ES = Country(code="ES", name="Spain", slug="spain", region="europe")
FR = Country(code="FR", name="France", slug="france", region="europe")
JP = Country(code="JP", name="Japan", slug="japan", region="asia")

A = make_provider("A", "AlphaSim", "alphasim", 300)
B = make_provider("B", "BetaNet", "betanet", 200)
J = make_provider("J", "JapanTel", "japantel", 150)


def es_entries():
    return [
        build_plan_entry(plan_id="A-eu-10", provider=A, name="Europe 10GB/30Days",
                         capacity_mb=10000, period_days=30, prices={"USD": 30.0, "EUR": 28.0}),
        build_plan_entry(plan_id="B-eu-15", provider=B, name="Europe 15GB/30Days",
                         capacity_mb=15000, period_days=30, prices={"USD": 35.0, "EUR": 33.0}),
        build_plan_entry(plan_id="A-eu-cap", provider=A, name="Europe 20GB 10Mbps",
                         capacity_mb=20000, period_days=30, prices={"USD": 40.0, "EUR": 38.0},
                         speed_limit=10000.0),  # capped
        build_plan_entry(plan_id="A-spain-only", provider=A, name="Spain 5GB/14Days",
                         capacity_mb=5000, period_days=14, prices={"USD": 15.0, "EUR": 14.0}),
    ]


def fr_entries():
    return [
        build_plan_entry(plan_id="A-eu-10", provider=A, name="Europe 10GB/30Days",
                         capacity_mb=10000, period_days=30, prices={"USD": 30.0, "EUR": 28.0}),
        build_plan_entry(plan_id="B-eu-15", provider=B, name="Europe 15GB/30Days",
                         capacity_mb=15000, period_days=30, prices={"USD": 35.0, "EUR": 33.0}),
        build_plan_entry(plan_id="A-eu-cap", provider=A, name="Europe 20GB 10Mbps",
                         capacity_mb=20000, period_days=30, prices={"USD": 40.0, "EUR": 38.0},
                         speed_limit=10000.0),  # capped
        build_plan_entry(plan_id="C-france-only", provider=make_provider("C", "GammaTel", "gammatel", 100),
                         name="France 10GB/30Days", capacity_mb=10000, period_days=30,
                         prices={"USD": 25.0, "EUR": 23.0}),
    ]


def jp_entries():
    return [
        build_plan_entry(plan_id="J-jp-10", provider=J, name="Japan 10GB/30Days",
                         capacity_mb=10000, period_days=30, prices={"USD": 40.0, "JPY": 6000}),
    ]


def _engine(tmp_path, slugs_payloads, itinerary, **eng_kw):
    with respx.mock(assert_all_called=False) as router:
        mock_geo_routes(router)
        for slug, payload in slugs_payloads:
            mock_plans_route(router, slug, payload)
        client = httpx.Client(timeout=5)
        eng = RecommendationEngine(client, tmp_path, use_cache=False, top_n=3, **eng_kw)
        eng.load(itinerary)
        yield eng
        client.close()


@pytest.fixture
def engine(tmp_path):
    es = build_plans_payload(es_entries())
    fr = build_plans_payload(fr_entries())
    it = Itinerary(countries=[ES, FR], days=9, data_gb=10.0, currency="EUR")
    yield from _engine(tmp_path, [("spain", es), ("france", fr)], it)


def test_intersection_membership(engine):
    assert "A-eu-10" in engine.plan_ids_by_slug["spain"]
    assert "A-eu-10" in engine.plan_ids_by_slug["france"]
    # Spain-only plan is not on France's page.
    assert "A-spain-only" in engine.plan_ids_by_slug["spain"]
    assert "A-spain-only" not in engine.plan_ids_by_slug["france"]


def test_report_speed_cap_excluded(engine):
    it = Itinerary(countries=[ES, FR], days=9, data_gb=10.0, currency="EUR")
    rep = engine.recommend(it)
    excluded_ids = {e["id"] for e in rep.excluded_speed_cap}
    assert "A-eu-cap" in excluded_ids
    a = next(e for e in rep.excluded_speed_cap if e["id"] == "A-eu-cap")
    assert any("speed limit" in r for r in a["reasons"])


def test_report_winners_meet_data_need_and_provider_diverse(engine):
    it = Itinerary(countries=[ES, FR], days=9, data_gb=10.0, currency="EUR")
    rep = engine.recommend(it)
    win_ids = {w.plan.id for w in rep.winners}
    assert "A-eu-10" in win_ids
    assert "B-eu-15" in win_ids
    assert all(w.meets_data_need for w in rep.winners)
    assert {w.plan.provider for w in rep.winners} == {"AlphaSim", "BetaNet"}


def test_report_totals(engine):
    it = Itinerary(countries=[ES, FR], days=9, data_gb=10.0, currency="EUR")
    rep = engine.recommend(it)
    # Intersection = {A-eu-10, B-eu-15, A-eu-cap}; A-eu-cap is speed-capped.
    assert rep.totals["pool_covering_all_destinations"] == 3
    assert rep.totals["speed_cap_excluded"] == 1


def test_provider_filter_works(tmp_path):
    es = build_plans_payload(es_entries())
    fr = build_plans_payload(fr_entries())
    it = Itinerary(countries=[ES, FR], days=9, data_gb=10.0, currency="EUR")
    with respx.mock(assert_all_called=False) as router:
        mock_geo_routes(router)
        mock_plans_route(router, "spain", es)
        mock_plans_route(router, "france", fr)
        client = httpx.Client(timeout=5)
        eng = RecommendationEngine(client, tmp_path, use_cache=False, top_n=5, provider="AlphaSim")
        eng.load(it)
        rep = eng.recommend(it)
        client.close()
    assert {w.plan.provider for w in rep.winners} <= {"AlphaSim"}
    assert rep.totals["eligible_candidates"] >= 1


def test_fallback_when_no_shared_plan(tmp_path):
    es = build_plans_payload(es_entries())
    jp = build_plans_payload(jp_entries())
    it = Itinerary(countries=[ES, JP], days=9, data_gb=10.0, currency="USD")
    with respx.mock(assert_all_called=False) as router:
        mock_geo_routes(router)
        mock_plans_route(router, "spain", es)
        mock_plans_route(router, "japan", jp)
        client = httpx.Client(timeout=5)
        eng = RecommendationEngine(client, tmp_path, use_cache=False, top_n=3)
        eng.load(it)
        rep = eng.recommend(it)
        client.close()
    # No plan appears on both the Spain and Japan pages -> no winners.
    assert rep.winners == []
    # Fallback offers the best purchasable plan per destination.
    assert {f["slug"] for f in rep.fallback} == {"spain", "japan"}
