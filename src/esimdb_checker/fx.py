"""Currency conversion with a disclosed, timestamped source.

Every plan in the API carries an USD price, so the requested currency is always
reachable: prefer a plan's native price in the requested currency, otherwise
convert its USD price using a documented FX rate (Frankfurter / ECB reference
rates). The rate, its source, and its date are recorded in the report's `fx`
block and disclosed to the user. If the rate cannot be obtained, cross-currency
plans are marked *not price-comparable* rather than silently relabelled.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import httpx

from . import config, http
from .models import Fx

# ---------------------------------------------------------------------------
# Supported currencies (single source of truth)
#
# This is the exact set the project can convert from USD — the Frankfurter /
# ECB reference currencies (see config.FX_BASE and /currencies). It is the
# authoritative list for *every* currency decision: price display, strategy
# ranking, budget comparison, saved-config validation, and wizard/CLI input
# validation. It is NOT an arbitrary list: only codes here can actually be
# fetched via config.fx_endpoint().
# ---------------------------------------------------------------------------
SUPPORTED_CURRENCIES: dict[str, str] = {
    "AUD": "Australian Dollar",
    "BRL": "Brazilian Real",
    "CAD": "Canadian Dollar",
    "CHF": "Swiss Franc",
    "CNY": "Chinese Renminbi Yuan",
    "CZK": "Czech Koruna",
    "DKK": "Danish Krone",
    "EUR": "Euro",
    "GBP": "British Pound",
    "HKD": "Hong Kong Dollar",
    "HUF": "Hungarian Forint",
    "IDR": "Indonesian Rupiah",
    "ILS": "Israeli New Shekel",
    "INR": "Indian Rupee",
    "ISK": "Icelandic Króna",
    "JPY": "Japanese Yen",
    "KRW": "South Korean Won",
    "MXN": "Mexican Peso",
    "MYR": "Malaysian Ringgit",
    "NOK": "Norwegian Krone",
    "NZD": "New Zealand Dollar",
    "PHP": "Philippine Peso",
    "PLN": "Polish Złoty",
    "RON": "Romanian Leu",
    "SEK": "Swedish Krona",
    "SGD": "Singapore Dollar",
    "THB": "Thai Baht",
    "TRY": "Turkish Lira",
    "USD": "United States Dollar",
    "ZAR": "South African Rand",
}


def normalize_currency(raw) -> str:
    """Trim and uppercase a currency code (empty string if not a string)."""
    if raw is None:
        return ""
    return str(raw).strip().upper()


def is_supported_currency(code) -> bool:
    return normalize_currency(code) in SUPPORTED_CURRENCIES


def currency_name(code) -> str | None:
    return SUPPORTED_CURRENCIES.get(normalize_currency(code))


def supported_currency_codes() -> list[str]:
    return sorted(SUPPORTED_CURRENCIES)


def supported_currency_list_text() -> str:
    return ", ".join(
        f"{c} ({SUPPORTED_CURRENCIES[c]})" for c in supported_currency_codes()
    )


def validate_currency_code(raw) -> tuple[str | None, str | None]:
    """Validate a currency code against the supported set.

    Returns ``(normalized_code, None)`` on success or ``(None, error)`` on
    failure. Case-insensitive. Rejects empty, numeric, and unknown codes with a
    clear explanation. This is the ONE validator used for wizard input, CLI
    flags, and saved-config load, so they can never diverge.
    """
    code = normalize_currency(raw)
    if not code:
        return None, "a currency code is required (e.g. EUR, USD, GBP)"
    if code.isdigit():
        return None, (
            f"'{code}' looks like a number, not a currency code — enter a 3-letter "
            f"ISO code. Supported: {', '.join(supported_currency_codes())}"
        )
    if code not in SUPPORTED_CURRENCIES:
        return None, (
            f"'{code}' is not a supported currency. Supported: "
            f"{', '.join(supported_currency_codes())}"
        )
    return code, None


def _cache_path(data_dir: Path, currency: str) -> Path:
    return data_dir / f"fx_usd_{currency.upper()}.json"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _fresh(data_dir: Path, currency: str) -> Fx | None:
    p = _cache_path(data_dir, currency)
    if not p.exists():
        return None
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    try:
        fetched = datetime.fromisoformat(doc["fetched_at"])
    except (KeyError, ValueError):
        return None
    age_h = (datetime.now(timezone.utc) - fetched).total_seconds() / 3600.0
    if age_h > config.FX_STALE_AFTER_HOURS:
        return None
    return Fx(
        base="USD",
        currency=currency.upper(),
        rate=float(doc["rate"]),
        source=doc.get("source", "frankfurter (ECB)"),
        date=doc.get("date", "unknown"),
        available=True,
        note="from cached rate",
    )


def _fetch(client: httpx.Client, currency: str, data_dir: Path) -> Fx:
    url = config.fx_endpoint(currency)
    body = http.http_get(client, url)
    doc = json.loads(body)
    rate = float(doc["rates"][currency.upper()])
    date = doc.get("date", "unknown")
    fx = Fx(
        base="USD",
        currency=currency.upper(),
        rate=rate,
        source="frankfurter (ECB reference rates)",
        date=date,
        available=True,
    )
    data_dir.mkdir(parents=True, exist_ok=True)
    _cache_path(data_dir, currency).write_text(
        json.dumps(
            {
                "base": "USD",
                "currency": currency.upper(),
                "rate": rate,
                "date": date,
                "source": fx.source,
                "fetched_at": _now_iso(),
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return fx


def fetch_fx(
    client: httpx.Client,
    currency: str,
    data_dir: Path,
    use_cache: bool = True,
) -> Fx:
    currency = currency.upper()
    if currency == "USD":
        return Fx(
            base="USD",
            currency="USD",
            rate=1.0,
            source="identity (requested currency is USD)",
            date=_now_iso(),
            available=True,
            note="no conversion needed",
        )
    if use_cache:
        cached = _fresh(data_dir, currency)
        if cached is not None:
            return cached
    try:
        return _fetch(client, currency, data_dir)
    except http.FetchError as exc:
        return Fx(
            base="USD",
            currency=currency,
            rate=None,
            source="frankfurter (ECB reference rates)",
            date="unavailable",
            available=False,
            note=(f"FX fetch failed ({exc}); plans not natively priced in {currency} "
                  f"are not price-comparable"),
        )
