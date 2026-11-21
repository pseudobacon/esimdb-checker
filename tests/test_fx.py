import pytest


def _fx(currency, rate, available=True):
    from esimdb_checker.models import Fx

    return Fx(base="USD", currency=currency, rate=rate, source="test", date="2024-06-01",
              available=available)


def test_convert_between_uses_usd_as_the_intermediate():
    from esimdb_checker.fx import convert_between

    eur, gbp = _fx("EUR", 0.92), _fx("GBP", 0.79)
    assert convert_between(20, "EUR", "GBP", eur, gbp) == pytest.approx(20 / 0.92 * 0.79)
    assert convert_between(10, "USD", "GBP", _fx("USD", 1.0), gbp) == pytest.approx(7.9)
    # same currency on either side is the identity, whatever the rates say
    assert convert_between(10, "eur", "EUR", eur, gbp) == 10


def test_convert_between_refuses_when_a_rate_is_missing():
    from esimdb_checker.fx import convert_between

    down = _fx("EUR", 0.92, available=False)
    assert convert_between(20, "EUR", "GBP", down, _fx("GBP", 0.79)) is None
    assert convert_between(20, "EUR", "GBP", _fx("EUR", None), _fx("GBP", 0.79)) is None
