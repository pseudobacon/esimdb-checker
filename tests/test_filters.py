"""Tests for speed-cap detection and purchasability / data-need filters."""

from __future__ import annotations

from esimdb_checker.filters import (
    after_allowance_cap,
    after_cap_phrase,
    always_throttled,
    has_speed_cap,
    meets_data_need_for_days,
    purchasable,
    speed_cap_label,
    speed_cap_reasons,
    speed_cap_signals,
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


# --- when does the throttle start? (the difference between buyable and useless) ---
def test_reduced_speed_alone_means_throttle_after_the_allowance():
    p = _plan(reduced_speed=128.0)
    assert after_allowance_cap(p)
    assert not always_throttled(p)
    assert speed_cap_signals(p) == ([], ["speed reduced to 128 kbps after cap"])
    assert after_cap_phrase(p) == "128 kbps after the allowance"
    assert speed_cap_label(p) == "then 128 kbps after the allowance"


def test_explicit_limit_is_always_throttled():
    p = _plan(speed_limit=1000.0)
    assert always_throttled(p)
    assert not after_allowance_cap(p)
    assert speed_cap_label(p) == "speed limited to 1 Mbps from first use"


def test_site_throttle_flag_is_always_throttled():
    p = _plan(possible_throttling=True)
    assert always_throttled(p)
    assert speed_cap_label(p) == "possible throttling (site flag)"


def test_a_hard_signal_outranks_an_after_cap_field():
    """reducedSpeed cannot vouch for a plan that also carries an explicit limit."""
    p = _plan(reduced_speed=128.0, speed_limit=1000.0)
    assert always_throttled(p)
    assert not after_allowance_cap(p)
    hard, after = speed_cap_signals(p)
    assert hard and not after


def test_text_alone_is_not_proof_of_an_after_allowance_cap():
    """'10GB then 384kbps' reads as after-cap to a human, but the plan gives no field to
    prove it; without proof the conservative reading (always throttled) is kept."""
    p = _plan(name="10GB then 384kbps")
    assert always_throttled(p)
    assert not after_allowance_cap(p)


def test_clean_plan_has_no_speed_warning():
    assert speed_cap_label(_plan()) is None
    assert after_cap_phrase(_plan()) is None


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
