"""Tests for name-based coverage scope resolution (labeling + ambiguity)."""

from __future__ import annotations

import pytest

from conftest import make_geo
from esimdb_checker.coverage import resolve_coverage


@pytest.fixture
def geo():
    return make_geo()


def test_global(geo):
    cov = resolve_coverage("Global 5GB/30Days", geo)
    assert cov.status == "verified"
    assert "ES" in cov.codes and "JP" in cov.codes


def test_europe_region(geo):
    cov = resolve_coverage("Europe (33 countries) 15GB/30Days", geo)
    assert cov.status == "verified"
    assert {"ES", "FR", "IT"} <= set(cov.codes)


def test_europe_subset_count_is_partial(geo):
    cov = resolve_coverage("Europe (2 countries) 5GB", geo)
    assert cov.status == "partial"
    assert {"ES", "FR", "IT"} <= set(cov.codes)


def test_single_country(geo):
    cov = resolve_coverage("Spain 5GB/7Days", geo)
    assert cov.status == "verified"
    assert cov.codes == ["ES"]


def test_multi_country(geo):
    cov = resolve_coverage("Spain + France 15GB", geo)
    assert cov.status == "verified"
    assert {"ES", "FR"} <= set(cov.codes)


def test_exclusion_is_partial(geo):
    cov = resolve_coverage("Europe no Italy 10GB", geo)
    assert cov.status == "partial"
    assert "IT" not in cov.codes
    assert "ES" in cov.codes


def test_unverified_generic(geo):
    cov = resolve_coverage("5GB / 30 Days eSIM", geo)
    assert cov.status == "unverified"
    assert cov.codes == []


def test_unverified_keeps_label(geo):
    cov = resolve_coverage("Mystery Plan 10GB", geo)
    assert cov.status == "unverified"
    assert cov.label
