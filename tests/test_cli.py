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
    mock_geo_routes,
    mock_plans_route,
)
from esimdb_checker.cli import main

P1 = make_provider("P1", "TestSim", "testsim", 500)


def _recommend_payload() -> dict:
    return build_plans_payload(
        [
            build_plan_entry(
                plan_id="eu-10",
                provider=P1,
                name="Europe 10GB/30Days",
                capacity_mb=10000,
                period_days=30,
                prices={"USD": 20.0, "EUR": 19.0},
            )
        ]
    )


def test_recommend_json_output(tmp_path, capsys):
    with respx.mock(assert_all_called=False) as router:
        mock_geo_routes(router)
        mock_plans_route(router, "spain", _recommend_payload())
        rc = main([
            "--data-dir", str(tmp_path),
            "recommend", "spain",
            "--json", "--no-cache", "--top", "2",
            "--days", "9", "--data-gb", "10", "--currency", "EUR",
        ])
    assert rc == 0
    out = capsys.readouterr().out
    data = json.loads(out)
    assert "winners" in data and "itinerary" in data and "totals" in data
    assert data["winners"], "expected at least one recommendation"
    assert data["winners"][0]["plan"]["id"] == "eu-10"


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
