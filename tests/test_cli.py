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
