"""Offline tests for the CLI (argparse + recommend path).

Each network-touching call passes an isolated ``--data-dir`` (a pytest tmp dir)
so the real ``.cache`` is never read or written.
"""

from __future__ import annotations

import json

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

from esimdb_checker.cli import main

P1 = make_provider("P1", "TestSim", "testsim", 500)


def _recommend_payload() -> dict:
    return build_plans_payload(
        [
            build_plan_entry(
                plan_id="eu-20",
                provider=P1,
                name="Europe 20GB/30Days",
                capacity_mb=20000,
                period_days=30,
                prices={"USD": 25.0, "EUR": 24.0},
            )
        ]
    )


def test_recommend_json_output(tmp_path, capsys):
    with respx.mock(assert_all_called=False) as router:
        mock_geo_routes(router)
        mock_fx_route(router, "EUR", 0.92)
        mock_plans_route(router, "spain", _recommend_payload())
        rc = main([
            "--data-dir", str(tmp_path),
            "recommend", "spain",
            "--json", "--refresh",
            "--days", "9", "--daily-data", "1.5", "--currency", "EUR",
        ])
    assert rc == 0
    out = capsys.readouterr().out
    data = json.loads(out)
    assert set(data["strategies"]) == {"local_only", "whole_trip", "hybrid"}
    assert data["itinerary"]["total_days"] == 9
    assert data["itinerary"]["legs"][0]["country"] == "Spain"
    assert data["recommended"] is not None
    assert data["recommended"]["family"] in {"local_only", "whole_trip", "hybrid"}
    assert data["recommended"]["plans"][0]["plan"] == "Europe 20GB/30Days"


def test_unknown_country_returns_nonzero(tmp_path, capsys):
    with respx.mock(assert_all_called=False) as router:
        mock_geo_routes(router)
        rc = main(["--data-dir", str(tmp_path), "recommend", "atlantis", "--json"])
    assert rc == 3
    assert "unrecognized country" in capsys.readouterr().err


def test_help_exits_zero():
    with pytest.raises(SystemExit) as exc:
        main(["--help"])
    assert exc.value.code == 0


def _itinerary_file(tmp_path, currency="GBP"):
    """A saved --itinerary file, which is what the budget path reads its currency from."""
    from esimdb_checker.itinerary import build_trip, save_itinerary_file
    from esimdb_checker.models import Country

    spain = Country(code="ES", name="Spain", slug="spain")
    trip = build_trip([(spain, 10, 0)], daily_data_gb=1.0, currency=currency)
    return save_itinerary_file(trip, tmp_path / "trip.itinerary.json", overwrite=True)


def test_budget_without_a_currency_belongs_to_the_itinerary(tmp_path, capsys):
    # --itinerary carries its own currency, which is not the --currency default. The
    # budget used to be assumed to be in the default, which invented a conversion and
    # then crashed on a function that does not exist (and on assigning to a frozen Trip).
    path = _itinerary_file(tmp_path, "GBP")
    with respx.mock(assert_all_called=False) as router:
        mock_geo_routes(router)
        mock_fx_route(router, "GBP", 0.79)
        mock_plans_route(router, "spain", _recommend_payload())
        rc = main([
            "--data-dir", str(tmp_path),
            "recommend", "--itinerary", str(path),
            "--budget", "20", "--json", "--refresh",
        ])
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert data["budget_status"]["budget"] == pytest.approx(20.0)
    assert data["budget_status"]["currency"] == "GBP"


def test_budget_currency_is_converted_into_the_trip_currency(tmp_path, capsys):
    path = _itinerary_file(tmp_path, "GBP")
    with respx.mock(assert_all_called=False) as router:
        mock_geo_routes(router)
        mock_fx_route(router, "GBP", 0.79)
        mock_fx_route(router, "EUR", 0.92)
        mock_plans_route(router, "spain", _recommend_payload())
        rc = main([
            "--data-dir", str(tmp_path),
            "recommend", "--itinerary", str(path),
            "--budget", "20", "--budget-currency", "EUR", "--json", "--refresh",
        ])
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    # 20 EUR -> USD at 1/0.92, then GBP at 0.79
    assert data["budget_status"]["budget"] == pytest.approx(20 / 0.92 * 0.79, abs=1e-6)
    assert data["budget_status"]["currency"] == "GBP"


def test_activate_lead_days_reaches_the_engine(tmp_path, capsys):
    path = _itinerary_file(tmp_path, "GBP")
    with respx.mock(assert_all_called=False) as router:
        mock_geo_routes(router)
        mock_fx_route(router, "GBP", 0.79)
        mock_plans_route(router, "spain", _recommend_payload())
        rc = main([
            "--data-dir", str(tmp_path),
            "recommend", "--itinerary", str(path),
            "--activate-lead-days", "1", "--json", "--refresh",
        ])
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert data["totals"]["activation_lead_days"] == 1
    capsys.readouterr()

    # And it is the default, not merely something the flag can ask for.
    assert main(["--data-dir", str(tmp_path), "recommend", "--itinerary", str(path),
                 "--json", "--refresh"]) == 0
    assert json.loads(capsys.readouterr().out)["totals"]["activation_lead_days"] == 1
    # A negative lead means "install after arriving", which is not a thing.
    with pytest.raises(SystemExit) as exc:
        main(["--data-dir", str(tmp_path), "recommend", "spain", "--days", "9",
              "--activate-lead-days", "-1", "--json"])
    assert exc.value.code == 2
    assert "must not be negative" in capsys.readouterr().err


def test_show_purchases_expands_ladder_rows(tmp_path, capsys):
    """The ladder priced every option; the install list used to follow only the answer."""
    from esimdb_checker.itinerary import build_trip, save_itinerary_file
    from esimdb_checker.models import Country

    es = Country(code="ES", name="Spain", slug="spain")
    jp = Country(code="JP", name="Japan", slug="japan")
    trip = build_trip([(es, 6, 0), (jp, 6, 0)], daily_data_gb=1.0, currency="GBP")
    path = save_itinerary_file(trip, tmp_path / "two.itinerary.json", overwrite=True)

    world = build_plan_entry(plan_id="glob-60", provider=P1, name="World 60GB/60Days",
                             capacity_mb=60000, period_days=60, prices={"USD": 30.0})

    def payload(local):
        return build_plans_payload([world, local])

    sp = build_plan_entry(plan_id="es-6", provider=P1, name="Spain 6GB/30Days",
                          capacity_mb=6000, period_days=30, prices={"USD": 5.0})
    jp6 = build_plan_entry(plan_id="jp-6", provider=P1, name="Japan 6GB/30Days",
                           capacity_mb=6000, period_days=30, prices={"USD": 6.0})

    def go(*extra):
        with respx.mock(assert_all_called=False) as router:
            mock_geo_routes(router)
            mock_fx_route(router, "GBP", 0.79)
            mock_plans_route(router, "spain", payload(sp))
            mock_plans_route(router, "japan", payload(jp6))
            rc = main(["--data-dir", str(tmp_path), "recommend", "--itinerary", str(path),
                       "--refresh", *extra])
        return rc, capsys.readouterr().out

    rc, out = go()
    assert rc == 0
    assert "What 1 eSIM would buy" not in out  # silent unless asked

    rc, out = go("--show-purchases", "1")
    assert rc == 0
    head = next(ln for ln in out.splitlines() if ln.startswith("  What 1 eSIM"))
    assert "would buy" in head
    block = out.split(head, 1)[1].split("RECOMMENDED", 1)[0]
    assert block.count("buy ") == 1 and "World 60GB/60Days" in block

    rc, out = go("--show-purchases", "all")
    assert rc == 0
    assert "What 1 eSIM would buy" in out and "What 2 eSIMs would buy" in out

    # A count nothing buys is an error naming the counts that do, not a silent no-op.
    with pytest.raises(SystemExit) as exc:
        go("--show-purchases", "5")
    assert exc.value.code == 2
    assert "no feasible option buys [5]" in capsys.readouterr().err

    with pytest.raises(SystemExit) as exc:
        go("--show-purchases", "two")
    assert exc.value.code == 2
    assert "purchase counts" in capsys.readouterr().err
