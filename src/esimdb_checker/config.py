"""Configuration: endpoints, headers, defaults, and data-dir resolution.

The endpoints below are the ones the esimdb.com Nuxt client fetches during a normal
page load (discovered empirically). They are *observed* public responses, not a
documented/stable public API — see docs/ARCHITECTURE.md for provenance and limits.
"""

from __future__ import annotations

import os
from pathlib import Path

BASE_URL = "https://esimdb.com"
LOCALE = "en"

# A normal desktop-browser User-Agent. The site's client uses a standard browser UA;
# we match it so the same structured responses are served.
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

TIMEOUT_SECONDS = 30.0
RETRIES = 3
RETRY_BACKOFF_FACTOR = 1.7
RETRY_STATUS_CODES = {429, 500, 502, 503, 504}


def countries_endpoint() -> str:
    return f"{BASE_URL}/api/client/countries?locale={LOCALE}"


def regions_endpoint() -> str:
    return f"{BASE_URL}/api/client/regions?locale={LOCALE}"


def plans_endpoint(slug: str) -> str:
    return f"{BASE_URL}/api/client/countries/{slug}/data-plans/folded?locale={LOCALE}"


def destination_page(slug: str) -> str:
    """Human-facing page where a plan for `slug` is listed."""
    return f"{BASE_URL}/{slug}"


def provider_page(country_slug: str, provider_slug: str) -> str:
    """Human-facing provider listing scoped to a destination."""
    return f"{BASE_URL}/{country_slug}/{provider_slug}"


def data_dir(default: str = ".cache") -> Path:
    """Resolve the snapshot directory: $ESIMDB_DATA_DIR > default (CWD-relative)."""
    override = os.environ.get("ESIMDB_DATA_DIR")
    p = Path(override) if override else Path(default)
    return p if p.is_absolute() else Path.cwd() / p
