"""Build, parse, and (de)serialize the itinerary.

A stop is "<destination> <days>" (space-separated, multiword names quoted at the
shell level). An empty stop "" is a 0-day transfer leg (no destination). Stops are
ordered; each entered day is one sequential travel-day slot. Transfer/connectivity
days attached to a leg are NOT charged (no data requirement). Trip totals are
derived from the legs: total_days = sum(leg.days), required_gb = total_days *
daily_data_gb.

Repeated visits to the same country are distinct legs.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from dataclasses import replace
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from .fx import validate_currency_code
from .models import Country, Leg, Trip, _gb


class StopError(ValueError):
    """Raised for malformed or conflicting stop/itinerary input."""


class CountryNotFoundError(StopError):
    """Raised when a destination cannot be resolved to a known country."""


def parse_stop(token: str) -> tuple[str | None, int, int, tuple[str, ...]]:
    """Parse one stop into (country_token_or_None, days, transfer_days, via_names).

    Forms:
      'Spain 4'      -> ('Spain', 4, 0, ())
      'united kingdom 5' (quoted) -> ('united kingdom', 5, 0, ())
      '' or ' '      -> (None, 0, 0, ())   # 0-day transfer leg
      'Spain 4 +2'   -> ('Spain', 4, 2, ())
      'Indonesia 14 +1 via Vietnam'
                   -> ('Indonesia', 14, 1, ('Vietnam',))

    Days must be a non-negative integer. '+N' (optional) is transfer days.
    'via <country>[, <country>]' (optional) names layover countries on the leg's
    transfer day(s): they cost no days or data, but a plan serving the leg must be
    listed on their pages.
    """
    if token is None:
        token = ""
    raw = token.strip()
    if raw == "":
        return None, 0, 0, ()

    # optional trailing 'via <countries>' marker (after the name, before or after +N)
    via_names: tuple[str, ...] = ()
    if " via " in raw:
        raw, via_part = raw.rsplit(" via ", 1)
        via_names = tuple(v.strip() for v in via_part.split(",") if v.strip())
        if not via_names:
            raise StopError(f"'via' needs at least one country name (got {token!r})")
        raw = raw.strip()

    # optional trailing '+N' transfer-days marker
    transfer = 0
    core = raw
    if " +" in raw:
        core, tr = raw.rsplit(" +", 1)
        try:
            transfer = int(tr.strip())
        except ValueError as exc:
            raise StopError(
                f"transfer-days marker must look like '+N' (got {tr!r} in {token!r})"
            ) from exc
        if transfer < 0:
            raise StopError(f"transfer days must be >= 0 (got {transfer})")

    parts = core.split()
    if len(parts) < 2:
        raise StopError(f"stop must look like '<destination> <days>' (got {token!r})")
    days_s = parts[-1]
    name = " ".join(parts[:-1]).strip()
    if not name:
        raise StopError(f"stop missing destination name (got {token!r})")
    try:
        days = int(days_s)
    except ValueError as exc:
        raise StopError(f"stop days must be an integer (got {days_s!r} in {token!r})") from exc
    if days < 0:
        raise StopError(f"stop days must be >= 0 (got {days})")
    return name, days, transfer, via_names


def parse_stops(
    stops: Iterable[str], geo
) -> list[tuple[Country | None, int, int, tuple[Country, ...]]]:
    """Parse and resolve stop tokens into ordered (country_or_None, days, transfer, via).

    Empty stops become (None, 0, 0, ()) transfer legs. Destination and via names are
    resolved through the shared Geo resolver (exact/alias first, then a single
    unambiguous substring candidate).
    """
    out: list[tuple[Country | None, int, int, tuple[Country, ...]]] = []
    for token in stops:
        name, days, transfer, via_names = parse_stop(token)
        via: tuple[Country, ...] = ()
        for v in via_names:
            c = geo.resolve_country(v)
            if c is None:
                cands = geo.candidates(v)
                if len(cands) == 1:
                    c = cands[0]
                else:
                    raise CountryNotFoundError(f"could not resolve via country {v!r}")
            via += (c,)
        if name is None:
            out.append((None, days, transfer, via))
            continue
        c = geo.resolve_country(name)
        if c is None:
            cands = geo.candidates(name)
            if len(cands) == 1:
                c = cands[0]
            else:
                raise CountryNotFoundError(f"could not resolve destination {name!r}")
        out.append((c, days, transfer, via))
    return out


def build_trip(
    stops: Iterable[tuple],
    daily_data_gb: float,
    currency: str,
    budget: float | None = None,
    transfer_data: float = 0.0,
    notes: Sequence[str] | None = None,
) -> Trip:
    """Build a Trip from ordered stops of (country_or_None, days, transfer_days)
    or (country_or_None, days, transfer_days, via_countries).

    Assigns each leg its start/end travel-day indices sequentially and computes
    required_gb = days * daily_data_gb (exact via Decimal). With transfer_data > 0,
    a leg's transfer (flying) days each use that fraction of the daily need and are
    charged into the leg's requirement; a 0-day leg's share is charged to the next
    destination leg (whose plan activates on those days), or to the previous one if
    it is the trip's tail. 0-day legs themselves always require 0 GB directly.
    """
    if not 0.0 <= transfer_data <= 1.0:
        raise StopError(f"transfer_data must be a fraction between 0 and 1 (got {transfer_data})")
    rate = Decimal(str(daily_data_gb))
    frac = Decimal(str(transfer_data))
    legs: list[Leg] = []
    cursor = 1
    for stop in stops:
        country, days, transfer = stop[0], stop[1], stop[2]
        via = tuple(stop[3]) if len(stop) > 3 else ()
        if days < 0:
            raise StopError(f"days must be >= 0 (got {days})")
        cursor += transfer  # transfer days before this leg consume calendar slots
        start = cursor
        end = cursor + days - 1 if days > 0 else cursor - 1  # 0-day -> empty interval
        if days > 0:
            required = float((Decimal(days) + transfer * frac) * rate)
        else:
            required = 0.0
        legs.append(Leg(country=country, days=days, start_day=start, end_day=end,
                        required_gb=required, transfer_days=transfer, via=via))
        if days > 0:
            cursor = end + 1
    if not legs:
        raise StopError("an itinerary needs at least one stop")
    for i, leg in enumerate(legs):
        if leg.days > 0 or not leg.transfer_days or not frac:
            continue
        charge = float(leg.transfer_days * frac * rate)
        target = next((j for j in range(i + 1, len(legs)) if legs[j].days > 0), None)
        if target is None:  # tail: the previous leg's plan must stay valid through them
            target = next((j for j in range(i - 1, -1, -1) if legs[j].days > 0), None)
        if target is None:
            raise StopError(
                "a transfer leg with flying-day data needs a destination leg to charge it to"
            )
        t = legs[target]
        legs[target] = replace(t, required_gb=t.required_gb + charge)
    return Trip(legs=tuple(legs), daily_data_gb=daily_data_gb,
                currency=currency.upper(), budget=budget, transfer_data=float(transfer_data),
                notes=tuple(str(n).strip() for n in (notes or ()) if str(n).strip()))


# ---- itinerary file I/O ---------------------------------------------------
def _validate_stops(stops: list[dict[str, Any]]) -> None:
    for i, s in enumerate(stops):
        if not isinstance(s, dict):
            raise StopError(f"stop #{i + 1} must be an object")
        if "days" not in s:
            raise StopError(f"stop #{i + 1} must have 'days'")
        d = s["days"]
        if isinstance(d, bool) or not isinstance(d, int) or d < 0:
            raise StopError(f"stop #{i + 1} 'days' must be an integer >= 0")
        country_raw = s.get("country")
        if country_raw is not None and (
            not isinstance(country_raw, str) or not country_raw.strip()
        ):
            raise StopError(f"stop #{i + 1} 'country' must be a string or null")
        via_raw = s.get("via")
        if via_raw is not None and (
            not isinstance(via_raw, list)
            or not all(isinstance(v, str) and v.strip() for v in via_raw)
        ):
            raise StopError(f"stop #{i + 1} 'via' must be a list of country names")


StopRow = tuple[str | None, int, int, list[str]]


def load_itinerary_file(path: Path) -> tuple[list[StopRow], dict[str, Any]]:
    """Load a saved itinerary. Returns (stops [(country, days, transfer, via_names)], settings)."""
    text = path.read_text(encoding="utf-8")
    try:
        obj = json.loads(text)
    except json.JSONDecodeError as exc:
        raise StopError(f"itinerary file is not valid JSON: {exc}") from exc
    if not isinstance(obj, dict):
        raise StopError("itinerary file must be a JSON object")
    stops_raw = obj.get("stops")
    if not isinstance(stops_raw, list) or not stops_raw:
        raise StopError("itinerary file must contain a non-empty 'stops' list")
    _validate_stops(stops_raw)
    stops = [
        (s.get("country"), int(s["days"]), int(s.get("transfer_days", 0)),
         [v.strip() for v in (s.get("via") or [])])
        for s in stops_raw
    ]

    settings: dict[str, Any] = {}
    if "daily_data_gb" in obj:
        d = _as_decimal(obj["daily_data_gb"], "daily_data_gb")
        if d is None or d <= 0:
            raise StopError("'daily_data_gb' must be a positive number")
        settings["daily_data_gb"] = float(d)
    else:
        raise StopError("itinerary file is missing 'daily_data_gb'")
    td = obj.get("transfer_data", 0)
    td_dec = _as_decimal(td, "transfer_data")
    if td_dec is None or not (Decimal(0) <= td_dec <= Decimal(1)):
        raise StopError("'transfer_data' must be a fraction between 0 and 1")
    settings["transfer_data"] = float(td_dec)
    budget = obj.get("budget")
    budget_currency_raw: Any | None = None
    if isinstance(budget, dict):
        amount = _as_decimal(budget.get("amount"), "budget.amount")
        if amount is None or amount <= 0:
            raise StopError("'budget.amount' must be a positive number")
        settings["budget"] = float(amount)
        budget_currency_raw = budget.get("currency")
    elif budget is not None:
        raise StopError("'budget' must be an object with 'amount' and 'currency'")

    # Effective currency: top-level 'currency' wins; otherwise the budget's.
    # It MUST be a supported code — a saved file with a bogus value (e.g. "15")
    # is reported exactly, never silently reinterpreted.
    currency_raw = obj.get("currency")
    effective = currency_raw if currency_raw not in (None, "") else budget_currency_raw
    if effective is None or (isinstance(effective, str) and not effective.strip()):
        raise StopError(
            "itinerary file is missing a valid 'currency' (a supported 3-letter "
            "code such as EUR or USD; see `esimdb-checker countries` for options)"
        )
    code, cur_err = validate_currency_code(effective)
    if cur_err is not None:
        raise StopError(f"itinerary file has an invalid currency {effective!r}: {cur_err}")
    settings["currency"] = code

    # Optional free-form notes (e.g. a same-day connection that costs no days):
    # validated, kept, and echoed back in the verbose report — never silently dropped.
    raw_notes = obj.get("notes")
    if raw_notes is None:
        settings["notes"] = []
    elif isinstance(raw_notes, list) and all(
        isinstance(n, str) and n.strip() for n in raw_notes
    ):
        settings["notes"] = [n.strip() for n in raw_notes]
    else:
        raise StopError("'notes' must be a list of non-empty strings")
    return stops, settings


def load_itinerary_trip(
    path: Path,
    geo,
    daily_data_gb: float | None = None,
    currency: str | None = None,
    budget: float | None = None,
    transfer_data: float | None = None,
) -> Trip:
    """Load a saved itinerary file and build a Trip (destinations resolved via geo).

    The file's daily_data_gb / currency / budget / transfer_data are authoritative
    unless the matching keyword overrides them (e.g. `recommend --itinerary FILE --currency X`).
    Currency (and any override) is validated against the supported set.
    """
    stops, settings = load_itinerary_file(path)
    resolved: list[tuple[Country | None, int, int, tuple[Country, ...]]] = []
    for name, days, transfer, via_names in stops:
        via: tuple[Country, ...] = ()
        for v in via_names:
            c = geo.resolve_country(v)
            if c is None:
                cands = geo.candidates(v)
                if len(cands) == 1:
                    c = cands[0]
                else:
                    raise CountryNotFoundError(
                        f"could not resolve via country {v!r} from saved itinerary"
                    )
            via += (c,)
        if name is None:
            resolved.append((None, days, transfer, via))
            continue
        c = geo.resolve_country(name)
        if c is None:
            cands = geo.candidates(name)
            if len(cands) == 1:
                c = cands[0]
            else:
                raise CountryNotFoundError(
                    f"could not resolve destination {name!r} from saved itinerary"
                )
        resolved.append((c, days, transfer, via))

    daily = settings["daily_data_gb"] if daily_data_gb is None else daily_data_gb
    cur = settings["currency"] if not currency else currency
    bud = settings.get("budget") if budget is None else budget
    tdata = settings.get("transfer_data", 0.0) if transfer_data is None else transfer_data
    if daily is None or daily <= 0:
        raise StopError("'daily_data_gb' must be a positive number")
    if not 0.0 <= tdata <= 1.0:
        raise StopError("'transfer_data' must be a fraction between 0 and 1")
    code, cur_err = validate_currency_code(cur)
    if cur_err is not None:
        raise StopError(f"invalid currency {cur!r}: {cur_err}")
    return build_trip(resolved, daily_data_gb=daily, currency=code, budget=bud,
                      transfer_data=tdata, notes=settings.get("notes"))


def save_itinerary_file(trip: Trip, path: Path, overwrite: bool = False) -> Path:
    """Write the itinerary to a JSON file. Refuses to overwrite unless overwrite=True."""
    if path.exists() and not overwrite:
        raise StopError(f"{path} already exists; pass overwrite=True or choose a different file")
    doc: dict[str, Any] = {
        "stops": [
            {
                "country": (leg.country.name if leg.country else None),
                "country_slug": (leg.country.slug if leg.country else None),
                "days": leg.days,
                "transfer_days": leg.transfer_days,
                **({"via": [c.name for c in leg.via]} if leg.via else {}),
            }
            for leg in trip.legs
        ],
        "daily_data_gb": _gb(trip.daily_data_gb),
        "transfer_data": _gb(trip.transfer_data),
        "currency": trip.currency,
    }
    if trip.budget is not None:
        doc["budget"] = {"amount": str(_gb(trip.budget)), "currency": trip.currency}
    if trip.notes:
        doc["notes"] = list(trip.notes)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


def _as_decimal(v: Any, label: str) -> Decimal | None:
    if v is None:
        return None
    try:
        return Decimal(str(v))
    except (InvalidOperation, ValueError) as exc:
        raise StopError(f"'{label}' must be a number (got {v!r})") from exc
