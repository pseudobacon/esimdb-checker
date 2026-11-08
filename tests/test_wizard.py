"""Offline tests for the wizard's shared state and plain (line-oriented) front-end.

Everything here is deterministic and network-free: destinations come from the
synthetic ``make_geo()`` fixture and user input is a scripted answer list.

``P(...)`` builds an answer script for the plain wizard's question order:
per leg (destination, days, transfer days), then finish, currency, budget,
daily data, "search?", and the optional save path.
"""

from __future__ import annotations

import json

import pytest
from conftest import feed, make_geo

from esimdb_checker.itinerary import StopError, load_itinerary_file, load_itinerary_trip
from esimdb_checker.itinerary import StopError as WizardStopError
from esimdb_checker.wizard import (
    DEFAULT_CURRENCY,
    DEFAULT_DAILY_DATA,
    WizardState,
    run_plain_wizard,
    state_from_file,
)


def P(*legs, finish="", currency="", budget="", daily="", search="y", save=""):
    answers: list[str] = []
    for dest, days, transfer in legs:
        answers += [dest, str(days), str(transfer)]
    return answers + [finish, currency, budget, daily, search, save]


# Tail for hand-written scripts: finish, currency, budget, daily.
TAIL = ["", "", "", ""]


def _geo_with_menu_letter_names():
    """Fake geo plus countries whose names collide with the menu letters."""
    from esimdb_checker.geo import Geo
    from esimdb_checker.models import Country
    g = make_geo()
    extra = [
        Country(code="TH", name="Thailand", slug="thailand", region="asia",
                subregions=[], popularity=90),
        Country(code="TR", name="Türkiye", slug="turkey", region="europe",
                subregions=[], popularity=88),
        Country(code="EE", name="Estonia", slug="estonia", region="europe",
                subregions=[], popularity=70),
        Country(code="RO", name="Romania", slug="romania", region="europe",
                subregions=[], popularity=75),
        Country(code="RU", name="Russia", slug="russia", region="europe",
                subregions=[], popularity=80),
        Country(code="DK", name="Denmark", slug="denmark", region="europe",
                subregions=[], popularity=65),
    ]
    return Geo(countries=list(g.countries) + extra,
               region_codes=dict(g.region_codes),
               region_alias=dict(g.region_alias))


def _run(answers, save_path=None, overwrite=False, state=None, geo=None):
    """Drive the plain wizard; returns (result, transcript_lines)."""
    out: list[str] = []
    result = run_plain_wizard(
        geo or make_geo(), state or WizardState(), feed(answers), out.append,
        save_path=save_path, overwrite=overwrite,
    )
    return result, out


def _review(out):
    return "\n".join(out).split("Confirm your itinerary:")[-1]


# ---------------------------------------------------------------------------
# Legs, per-destination durations, transfer days
# ---------------------------------------------------------------------------
def test_basic_two_leg_itinerary():
    result, _ = _run(P(("spain", 4, 0), ("france", 7, 0)))
    assert not result.cancelled
    trip = result.trip
    assert [(leg.country.name, leg.days) for leg in trip.legs] == [("Spain", 4), ("France", 7)]
    # per-destination durations produce sequential, non-overlapping day spans
    assert (trip.legs[0].start_day, trip.legs[0].end_day) == (1, 4)
    assert (trip.legs[1].start_day, trip.legs[1].end_day) == (5, 11)
    assert trip.total_days == 11
    assert trip.currency == "EUR"
    assert trip.daily_data_gb == DEFAULT_DAILY_DATA
    assert trip.budget is None
    assert abs(trip.required_gb - 16.5) < 1e-6


def test_transfer_days_asked_per_leg_and_not_charged():
    # 3 uncharged transfer days precede Spain, 2 precede France.
    result, out = _run(P(("spain", 4, 3), ("france", 5, 2)))
    trip = result.trip
    assert trip.legs[0].transfer_days == 3
    assert trip.legs[1].transfer_days == 2
    # charged days are the destination days only ...
    assert trip.total_days == 9
    assert abs(trip.required_gb - 13.5) < 1e-6
    # ... but the transfer days still shift the calendar spans shown to the user
    assert (trip.legs[0].start_day, trip.legs[0].end_day) == (4, 7)
    assert (trip.legs[1].start_day, trip.legs[1].end_day) == (10, 14)
    assert "4-7" in _review(out) and "10-14" in _review(out)


def test_standalone_transfer_leg_is_not_charged():
    answers = ["spain", "4", "", "t", "1", "france", "5", ""] + TAIL + ["y"]
    result, _ = _run(answers)
    trip = result.trip
    assert len(trip.legs) == 3
    assert trip.legs[1].is_transfer
    assert trip.legs[1].required_gb == 0.0
    assert trip.total_days == 9
    assert abs(trip.required_gb - 13.5) < 1e-6


def test_edit_and_remove_legs_before_search():
    # spain 4 + france 7 -> edit leg 2 to 10 days -> remove leg 1
    answers = ["spain", "4", "", "france", "7", "",
               "e", "2", "10", "0",
               "r", "1"] + TAIL + ["y"]
    result, _ = _run(answers)
    trip = result.trip
    assert [(leg.country.name, leg.days) for leg in trip.legs] == [("France", 10)]
    assert trip.total_days == 10


def test_repeated_country_is_a_separate_leg():
    result, _ = _run(P(("spain", 3, 0), ("spain", 2, 0)))
    assert [leg.days for leg in result.trip.legs] == [3, 2]
    assert result.trip.total_days == 5


def test_unknown_destination_is_reasked_in_place():
    answers = ["atlantis", "spain", "4", ""] + TAIL + ["y"]
    result, out = _run(answers)
    assert result.trip.legs[0].country.name == "Spain"
    assert any("no country matches" in line for line in out)


def test_bad_days_are_reasked_without_losing_the_destination():
    answers = ["spain", "four", "4", ""] + TAIL + ["y"]
    result, out = _run(answers)
    assert result.trip.legs[0].days == 4
    assert any("not a whole number" in line for line in out)


def test_cancel_at_confirm_searches_nothing():
    result, out = _run(P(("spain", 3, 0), search="n"))
    assert result.cancelled
    assert result.trip is None
    assert any("cancel" in line.lower() for line in out)


# ---------------------------------------------------------------------------
# Currency: default, interactive change, validation, budget independence
# ---------------------------------------------------------------------------
def test_currency_defaults_to_eur():
    result, _ = _run(P(("spain", 4, 0)))
    assert result.trip.currency == "EUR" == DEFAULT_CURRENCY


def test_currency_changed_interactively_to_gbp():
    result, _ = _run(P(("spain", 4, 0), currency="GBP"))
    assert result.trip.currency == "GBP"


def test_currency_entered_lowercase_is_normalized():
    result, out = _run(P(("spain", 4, 0), currency="gbp"))
    assert result.trip.currency == "GBP"
    assert not any("not a supported currency" in line for line in out)


def test_numeric_currency_is_rejected_and_reasked_in_place():
    answers = ["spain", "4", "", "", "15", "EUR", "", "", "y"]
    result, out = _run(answers)
    assert result.trip.currency == "EUR"          # only a valid code was accepted
    assert "'15' looks like a number, not a currency code" in " ".join(out)


def test_unknown_currency_is_rejected_with_the_supported_list():
    answers = ["spain", "4", "", "", "XYZ", "EUR", "", "", "y"]
    result, out = _run(answers)
    assert result.trip.currency == "EUR"
    joined = " ".join(out)
    assert "'XYZ' is not a supported currency" in joined
    assert "Supported:" in joined and "USD" in joined   # the list was shown


def test_budget_blank_keeps_the_chosen_currency():
    result, _ = _run(P(("spain", 4, 0), currency="USD"))
    assert result.trip.currency == "USD"
    assert result.trip.budget is None


def test_budget_is_held_in_the_chosen_currency():
    result, _ = _run(P(("spain", 4, 0), currency="USD", budget="250"))
    assert result.trip.currency == "USD"
    assert result.trip.budget == 250


def test_invalid_budget_is_reasked_in_place():
    answers = ["spain", "4", "", "", "USD", "cheap", "300", "", "y"]
    result, out = _run(answers)
    assert result.trip.budget == 300
    assert any("budget must be a number" in line for line in out)


# ---------------------------------------------------------------------------
# Daily data: presets, literal values, default, large trip
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("answer,expected", [
    ("Higher", 2.0),
    ("higher usage", 2.0),
    ("2", 2.0),
    ("2.0", 2.0),
])
def test_two_gb_per_day_from_preset_or_literal(answer, expected):
    result, _ = _run(P(("spain", 4, 0), daily=answer))
    assert result.trip.daily_data_gb == expected


def test_daily_data_defaults_to_balanced_1_5():
    result, _ = _run(P(("spain", 4, 0)))
    assert result.trip.daily_data_gb == 1.5


def test_daily_data_economy_preset_is_one_gb():
    result, _ = _run(P(("spain", 4, 0), daily="Economy"))
    assert result.trip.daily_data_gb == 1.0


def test_34_day_trip_at_2gb_needs_68gb():
    result, out = _run(P(("spain", 20, 0), ("france", 14, 0), daily="2"))
    trip = result.trip
    assert trip.total_days == 34
    assert trip.daily_data_gb == 2.0
    assert abs(trip.required_gb - 68.0) < 1e-9
    assert "68 GB" in " ".join(out)               # stated back to the user


def test_invalid_daily_data_is_reasked_in_place():
    answers = ["spain", "4", "", "", "", "", "banana", "3", "y"]
    result, out = _run(answers)
    assert result.trip.daily_data_gb == 3.0
    assert any("GB/day" in line and "enter a number" in line for line in out)


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------
def test_saved_itinerary_round_trips(tmp_path):
    path = tmp_path / "itinerary.json"
    result, _ = _run(P(("spain", 4, 2), ("france", 7, 0),
                       currency="USD", budget="250", daily="2"), save_path=path)
    assert result.save_path == path and path.exists()

    stops, settings = load_itinerary_file(path)
    assert [(n, d) for n, d, _ in stops] == [("Spain", 4), ("France", 7)]
    assert [t for _, _, t in stops] == [2, 0]
    assert settings["currency"] == "USD"
    assert settings["daily_data_gb"] == 2.0
    assert settings["budget"] == 250

    trip2 = load_itinerary_trip(path, make_geo())
    assert trip2.currency == "USD" and trip2.total_days == 11
    assert abs(trip2.required_gb - 22.0) < 1e-9


def test_existing_file_is_not_overwritten_without_confirmation(tmp_path):
    path = tmp_path / "itinerary.json"
    path.write_text('{"keep": "me"}', encoding="utf-8")
    result, out = _run(P(("spain", 4, 0)), save_path=path)   # EOF at the overwrite prompt
    assert not result.cancelled
    assert result.save_path is None
    assert json.loads(path.read_text()) == {"keep": "me"}
    assert any("already exists" in line for line in out)


def test_existing_file_overwritten_on_confirmation(tmp_path):
    path = tmp_path / "itinerary.json"
    path.write_text('{"keep": "me"}', encoding="utf-8")
    # With --save-file the save path is not asked, so the last answer confirms the overwrite.
    answers = P(("spain", 4, 0))[:-1] + ["y"]
    result, _ = _run(answers, save_path=path)
    assert result.save_path == path
    assert "stops" in json.loads(path.read_text())


def test_wizard_can_reuse_and_edit_a_saved_itinerary(tmp_path):
    original = tmp_path / "trip.json"
    original.write_text(json.dumps({
        "stops": [
            {"country": "Spain", "country_slug": "spain", "days": 4, "transfer_days": 0},
            {"country": "France", "country_slug": "france", "days": 7, "transfer_days": 1},
        ],
        "daily_data_gb": 2,
        "currency": "USD",
    }), encoding="utf-8")

    state = state_from_file(original, make_geo())
    assert state.currency == "USD" and state.daily_data_gb == 2
    assert [(leg.country.name, leg.days, leg.transfer_days) for leg in state.legs] == [
        ("Spain", 4, 0), ("France", 7, 1)
    ]
    # Continue in the same session: add one leg and change the currency.
    answers = ["japan", "5", ""] + TAIL[:1] + ["EUR"] + TAIL[2:] + ["y"]
    result, _ = _run(answers, state=state)
    trip = result.trip
    assert [leg.country.name for leg in trip.legs] == ["Spain", "France", "Japan"]
    assert trip.currency == "EUR"
    assert trip.legs[1].transfer_days == 1


# ---------------------------------------------------------------------------
# Saved-config currency validation (the "15" defect)
# ---------------------------------------------------------------------------
def _broken(tmp_path, currency):
    path = tmp_path / "itinerary.json"
    path.write_text(json.dumps({
        "stops": [{"country": "Taiwan", "country_slug": "taiwan", "days": 4, "transfer_days": 0}],
        "daily_data_gb": 1.5,
        "currency": currency,
    }), encoding="utf-8")
    return path


def test_saved_numeric_currency_is_reported_not_guessed(tmp_path):
    with pytest.raises(StopError) as exc:
        load_itinerary_file(_broken(tmp_path, "15"))
    msg = str(exc.value)
    assert "invalid currency '15'" in msg
    assert "looks like a number" in msg
    assert "Supported:" in msg


def test_saved_unknown_currency_is_reported(tmp_path):
    with pytest.raises(StopError) as exc:
        load_itinerary_trip(_broken(tmp_path, "XYZ"), make_geo())
    assert "'XYZ' is not a supported currency" in str(exc.value)
    assert "Supported:" in str(exc.value)


def test_saved_missing_currency_is_reported(tmp_path):
    path = tmp_path / "itinerary.json"
    path.write_text(json.dumps({
        "stops": [{"country": "Spain", "days": 4}], "daily_data_gb": 1.5,
    }), encoding="utf-8")
    with pytest.raises(StopError) as exc:
        load_itinerary_file(path)
    assert "missing a valid 'currency'" in str(exc.value)


def test_valid_saved_currency_is_accepted_case_insensitively(tmp_path):
    path = _broken(tmp_path, "eur")
    _, settings = load_itinerary_file(path)
    assert settings["currency"] == "EUR"


# ---------------------------------------------------------------------------
# Non-interactive / robustness
# ---------------------------------------------------------------------------
def test_exhausted_input_fails_with_an_explanation():
    result, out = _run([])                       # stdin closed immediately
    assert result.cancelled and result.trip is None
    assert any("EOF" in line or "no more input" in line for line in out)


def test_typing_q_aborts_immediately():
    result, out = _run(["spain", "q"])
    assert result.cancelled and result.trip is None
    assert any("aborted by user" in line for line in out)


def test_plain_mode_emits_no_terminal_control_sequences():
    _, out = _run(P(("spain", 4, 1), ("france", 7, 0), currency="EUR",
                    budget="100", daily="2"))
    transcript = "\n".join(out)
    assert "\x1b" not in transcript             # no cursor / alternate-screen escapes
    assert "Itinerary so far:" in transcript


def test_review_lists_each_leg_once_without_duplicate_append():
    _, out = _run(P(("spain", 4, 0), ("france", 7, 0), ("japan", 3, 0)))
    review = _review(out)
    for name in ("Spain", "France", "Japan"):
        assert review.count(name) == 1, review
    assert review.count("TOTAL:") == 1


def test_wizard_reuses_the_itinerary_stop_error():
    assert WizardStopError is StopError


# --------------------------------------------------------------------------
# The save prompt asks for a path, so yes/no answers must not become filenames.
# --------------------------------------------------------------------------
def test_bare_yes_at_the_save_prompt_saves_the_default_name(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result, _ = _run(P(("spain", 4, 0), save="y"))
    assert result.save_path.resolve() == tmp_path / "itinerary.json"
    assert (tmp_path / "itinerary.json").exists()
    assert not (tmp_path / "y").exists()


def test_answering_no_at_the_save_prompt_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result, _ = _run(P(("spain", 4, 0), save="n"))
    assert result.save_path is None
    assert not (tmp_path / "n").exists()
    assert not (tmp_path / "itinerary.json").exists()


def test_a_path_answer_still_saves_where_the_user_asked(tmp_path):
    target = tmp_path / "sub" / "trip.json"
    result, _ = _run(P(("spain", 4, 0), save=str(target)))
    assert result.save_path == target and target.exists()


# --------------------------------------------------------------------------
# Menu words are matched exactly: country names must never become commands.
# --------------------------------------------------------------------------
@pytest.mark.parametrize("country,expected", [
    ("thailand", "Thailand"), ("Thailand", "Thailand"), ("turkey", "Türkiye"),
    ("togo", None),                     # unknown name: reported, not taken as a command
    ("estonia", "Estonia"), ("romania", "Romania"), ("russia", "Russia"),
    ("denmark", "Denmark"),
])
def test_country_names_that_start_with_a_menu_letter_stay_destinations(country, expected):
    """Once one leg exists, a bare country name must not read as t/e/r/d."""
    answers = ["spain", "3", "0", country, "5", "0"] + TAIL + ["y", ""]
    result, out = _run(answers, geo=_geo_with_menu_letter_names())
    transcript = "\n".join(out)
    if expected is None:
        assert "no country matches" in transcript
        assert not any(leg.country is None for leg in result.trip.legs)
        return
    assert not result.cancelled
    legs = result.trip.legs
    assert not [leg for leg in legs if leg.country is None], f"{country} became a transfer marker"
    assert [leg.country.name for leg in legs] == ["Spain", expected], legs
    assert result.trip.total_days == 8


def test_the_menu_words_still_work_when_spelled_out():
    answers = ["spain", "3", "0",
               "transfer", "2",          # t
               "edit", "1", "6", "0",    # e
               "remove", "2",            # r
               "add", "france", "4", "1"] + TAIL + ["y", ""]
    result, out = _run(answers)
    assert not result.cancelled
    legs = result.trip.legs
    assert [leg.country.name if leg.country else "transfer" for leg in legs] == [
        "Spain", "France"]
    assert legs[0].days == 6 and legs[1].days == 4 and legs[1].transfer_days == 1


def test_bare_n_finishes_the_destination_stage():
    result, _ = _run(["spain", "3", "0", "n"] + TAIL + ["y", ""])
    assert not result.cancelled and result.trip.total_days == 3
