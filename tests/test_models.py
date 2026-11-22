"""Capacity labels: the site's GB is 1000 MB, and labels must show what it sells."""

from __future__ import annotations

from esimdb_checker.models import Plan, _gb_label


def _plan(capacity_mb: int, data_cap_per: str | None = None, **kw) -> Plan:
    return Plan(
        id="p", name="n", provider="P", provider_slug="p",
        prices={"USD": 1.0}, capacity_mb=capacity_mb, period_days=30,
        data_cap_per=data_cap_per, **kw,
    )


def test_gb_label_uses_the_site_base_1000():
    # Advertised figures: 3000 MB is a "3 GB" plan, 24000 a "24 GB" plan.
    assert _gb_label(3000) == "3 GB"
    assert _gb_label(24000) == "24 GB"
    assert _gb_label(100000) == "100 GB"
    # Sub-GB figures keep one decimal: 1500 MB is sold as "1.5 GB".
    assert _gb_label(1500) == "1.5 GB"
    assert _gb_label(1300) == "1.3 GB"


def test_sub_gb_packages_stay_in_mb():
    # The 500 MB packages are specified in MB and must not become "0.5 GB".
    assert _gb_label(500) == "500 MB"
    assert _gb_label(86) == "86 MB"


def test_capacity_label_matches_the_advertised_figure():
    assert _plan(3000).capacity_label() == "3 GB"
    assert _plan(0).capacity_label() == "Unlimited"
    daily = _plan(1500, data_cap_per="day")
    assert daily.capacity_label() == "1.5 GB/day"
    assert daily.data_cap_label() == "up to 1.5 GB/day"
    assert _plan(3000).data_cap_label() == ""
