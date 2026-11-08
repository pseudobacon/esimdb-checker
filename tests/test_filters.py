"""Tests for speed-cap detection and purchasability / data-need filters."""

from __future__ import annotations

from esimdb_checker.filters import (
    has_speed_cap,
    meets_data_need_for_days,
    purchasable,
    speed_cap_reasons,
)
from esimdb_checker.models import Plan


def _plan(**kw):
    base = dict(id="p", name="Test 10GB", provider="A", provider_slug="a",
                capacity_mb=10000, period_days=30, prices={"USD": 50.0})
    base.update(kw)
    return Plan(**base)


def test_explicit_speed_limit_is_kbps():
    p = _plan(speed_limit=5000.0)  # 5000 kbps == 5 Mbps
    assert has_speed_cap(p)
    assert any("5 Mbps" in r for r in speed_cap_reasons(p))


def test_reduced_speed_is_capped():
    assert has_speed_cap(_plan(reduced_speed=50.0))


def test_possible_throttling_is_capped():
    assert has_speed_cap(_plan(possible_throttling=True))


def test_throttling_text_is_capped():
    p = _plan(name="10GB then 384kbps after cap")
    assert has_speed_cap(p)


def test_clean_plan_is_uncapped():
    assert not has_speed_cap(_plan())


def test_purchasable_requires_positive_price():
    assert purchasable(_plan(prices={"USD": 10.0}))[0] is True
    assert purchasable(_plan(prices={}))[0] is False
    assert purchasable(_plan(prices={"USD": 0.0}))[0] is False


def test_purchasable_eur_only_is_ok():
    # No USD price, but a positive EUR price => still purchasable.
    ok, _ = purchasable(_plan(prices={"EUR": 5.5}))
    assert ok is True


def test_new_user_only_adds_caveat_but_still_purchasable():
    ok, caveats = purchasable(_plan(new_user_only=True, prices={"USD": 10.0}))
    assert ok is True
    assert any("new-user-only" in c for c in caveats)


def test_meets_data_need_finite():
    p = _plan(capacity_mb=10000)  # 10GB
    assert meets_data_need_for_days(p, 10.0, 9) is True
    assert meets_data_need_for_days(p, 15.0, 9) is False


def test_meets_data_need_daily_cap_scales():
    p = _plan(capacity_mb=5000, data_cap_per="day", period_days=30)  # 5GB/day
    assert meets_data_need_for_days(p, 10.0, 9) is True   # 45GB over 9 days
    assert meets_data_need_for_days(p, 50.0, 9) is False


def test_unlimited_always_meets():
    p = _plan(capacity_mb=0)
    assert meets_data_need_for_days(p, 1000.0, 9) is True
