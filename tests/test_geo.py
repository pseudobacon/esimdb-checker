"""Tests for country/region resolution (offline, incl. a mocked Geo.load)."""

from __future__ import annotations

import httpx
import respx

from conftest import make_geo, mock_geo_routes
from esimdb_checker.geo import Geo


def test_resolve_by_name_code_slug():
    g = make_geo()
    assert g.resolve_country("spain").code == "ES"
    assert g.resolve_country("Spain").code == "ES"
    assert g.resolve_country("ES").code == "ES"
    assert g.resolve_country("FR").code == "FR"


def test_alias_resolves():
    g = make_geo()
    assert g.resolve_country("uk").code == "GB"


def test_unknown_returns_none():
    g = make_geo()
    assert g.resolve_country("atlantis") is None


def test_region_codes():
    g = make_geo()
    assert g.region_codes_for("europe") == {"ES", "FR", "IT"}


def test_region_for_alias():
    g = make_geo()
    assert g.region_for_alias("europe") == "europe"
    assert g.region_for_alias("eu") == "europe"


def test_all_country_tokens_include_names_and_codes():
    g = make_geo()
    toks = g.all_country_tokens()
    assert "spain" in toks
    assert "es" in toks  # codes are lowercased in the token set


def test_load_offline(tmp_path):
    with respx.mock(assert_all_called=False) as router:
        mock_geo_routes(router)
        client = httpx.Client(timeout=5)
        g = Geo.load(client, tmp_path, use_cache=False)
        client.close()
    assert any(c.code == "ES" for c in g.countries)
    assert g.region_codes_for("europe") == {"ES", "FR", "IT"}
