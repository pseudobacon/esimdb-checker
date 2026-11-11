"""Headless tests for the keyboard-only Textual wizard (App.run_test / Pilot).

Every interaction here is a key press: there are no buttons to click, and the
suite asserts that. `pilot.press` delivers real Textual key events, so the same
bindings a user drives are the ones under test.
"""

from __future__ import annotations

import asyncio
import json

from conftest import make_geo
from textual.widgets import Button, Input, Static

from esimdb_checker.wizard import (
    DATA_PRESETS,
    WizardApp,
    WizardResult,
    WizardState,
    run_tui_wizard,
)

SIZE = (100, 34)


def run(coro):
    asyncio.run(coro)


# --------------------------------------------------------------------------
# helpers - all keyboard
# --------------------------------------------------------------------------
def text(app, widget_id: str) -> str:
    return str(app.query_one(f"#{widget_id}", Static).render().plain)


def body_text(app) -> str:
    return text(app, "body")


def status_text(app) -> str:
    return text(app, "status")


def hint_text(app) -> str:
    return text(app, "hint")


def progress_text(app) -> str:
    return text(app, "progress")


def answer(app) -> Input:
    return app.query_one("#answer", Input)


def answer_open(app) -> bool:
    """True while a question is open for typing."""
    return not answer(app).disabled


async def press(pilot, *presses: str, pause: float = 0.06) -> None:
    for key in presses:
        await pilot.press(key)
        await pilot.pause(pause)


async def type_text(pilot, value: str, pause: float = 0.02) -> None:
    await press(pilot, *list(value), pause=pause)


async def answer_with(pilot, value: str, pause: float = 0.02) -> None:
    """Type an answer and confirm it with Enter."""
    await type_text(pilot, value, pause=pause)
    await press(pilot, "enter")


async def add_leg(pilot, dest: str, days: str, transfer: str = "0") -> None:
    await press(pilot, "a")
    await answer_with(pilot, dest)
    await answer_with(pilot, days)
    await answer_with(pilot, transfer)


async def to_review(pilot, legs=(("spain", "4"),), currency="EUR", budget="200",
                    data_steps: int = 0) -> None:
    """Run the whole interview up to (but not including) the search."""
    for dest, days in legs:
        await add_leg(pilot, dest, days)
    await press(pilot, "enter")                  # -> currency
    await answer_with(pilot, currency)
    await answer_with(pilot, budget)
    for _ in range(data_steps):                  # move the data-level choice
        await press(pilot, "down")
    await press(pilot, "enter")                  # -> review


# --------------------------------------------------------------------------
# structure: no buttons, no mouse
# --------------------------------------------------------------------------
def test_the_wizard_has_no_button_widgets():
    async def scenario():
        app = WizardApp(make_geo(), WizardState(), None, False)
        async with app.run_test(size=SIZE) as pilot:
            await pilot.pause()
            assert len(app.query(Button)) == 0
            # only static text plus the single answer field is on screen
            kinds = {type(w).__name__ for w in app.query("*")}
            assert kinds <= {"Screen", "Static", "Input"}, kinds
            assert len(app.query(Input)) == 1
            # ...and in every later context too
            await add_leg(pilot, "spain", "4")
            assert len(app.query(Button)) == 0
            await press(pilot, "enter")
            assert len(app.query(Button)) == 0
    run(scenario())


def test_the_tui_never_asks_the_terminal_to_capture_the_mouse(monkeypatch):
    """mouse=False means no ?1000h/?1003h: clicks stay with the terminal."""
    seen = {}
    real_run = WizardApp.run

    def spy(self, *args, **kwargs):
        seen.update(kwargs)
        self.result = WizardResult(trip=None, cancelled=True, save_path=None)

    monkeypatch.setattr(WizardApp, "run", spy)
    result = run_tui_wizard(make_geo(), WizardState(), None, False)
    assert seen.get("mouse") is False, seen
    assert result.cancelled
    assert real_run is not None


def test_the_hint_line_shows_the_keys_for_the_current_context():
    async def scenario():
        app = WizardApp(make_geo(), WizardState(), None, False)
        async with app.run_test(size=SIZE) as pilot:
            await pilot.pause()
            hint = hint_text(app)
            for key in ("a add", "e edit", "d delete", "Enter continue",
                        "Esc cancel"):
                assert key in hint, hint
            # Esc is the documented way back; Alt+Left works but is not advertised.
            assert "Alt+Left" not in hint, hint
            await press(pilot, "a")
            assert "Enter next" in hint_text(app)
            await press(pilot, "escape")
            assert "Enter continue" in hint_text(app)
    run(scenario())


# --------------------------------------------------------------------------
# a complete session, keyboard only
# --------------------------------------------------------------------------
def test_complete_keyboard_session_builds_the_itinerary():
    async def scenario():
        app = WizardApp(make_geo(), WizardState(), None, False)
        async with app.run_test(size=SIZE) as pilot:
            await pilot.pause()
            await add_leg(pilot, "spain", "4")
            await add_leg(pilot, "france", "7", "2")
            await press(pilot, "enter")                       # preferences: currency
            await answer_with(pilot, "GBP")
            await answer_with(pilot, "200")                   # budget
            await press(pilot, "down")                        # Higher usage (2 GB/day)
            await press(pilot, "enter")                       # review
            assert "GBP" in body_text(app) and "200" in body_text(app)
            await press(pilot, "enter")                       # search, no confirmation
            await pilot.pause()
        trip = app.result.trip
        assert app.result.cancelled is False
        assert [(leg.country.name, leg.days, leg.transfer_days) for leg in trip.legs] == [
            ("Spain", 4, 0), ("France", 7, 2)]
        assert trip.currency == "GBP" and trip.budget == 200.0
        assert trip.daily_data_gb == 2.0
        assert [leg.required_gb for leg in trip.legs] == [8.0, 14.0]
    run(scenario())


def test_repeated_countries_stay_separate_ordered_legs():
    async def scenario():
        app = WizardApp(make_geo(), WizardState(), None, False)
        async with app.run_test(size=SIZE) as pilot:
            await pilot.pause()
            await add_leg(pilot, "spain", "4")
            await add_leg(pilot, "france", "7")
            await add_leg(pilot, "spain", "3")
            await press(pilot, "enter")
            await answer_with(pilot, "EUR")
            await answer_with(pilot, "")                      # no budget
            await press(pilot, "enter")                       # keep Balanced
            await press(pilot, "enter")
            await press(pilot, "y")
        legs = app.result.trip.legs
        assert [leg.country.name for leg in legs] == ["Spain", "France", "Spain"]
        assert [(leg.start_day, leg.end_day) for leg in legs] == [(1, 4), (5, 11), (12, 14)]
    run(scenario())


# --------------------------------------------------------------------------
# choice questions: Up/Down + Enter
# --------------------------------------------------------------------------
def test_data_level_is_chosen_with_up_down_and_enter():
    async def scenario():
        app = WizardApp(make_geo(), WizardState(), None, False)
        async with app.run_test(size=SIZE) as pilot:
            await pilot.pause()
            await add_leg(pilot, "spain", "10")
            await press(pilot, "enter")
            await answer_with(pilot, "EUR")
            await answer_with(pilot, "")
            assert ">" in body_text(app)                      # a cursor is shown
            assert "GB/day" in body_text(app)
            await press(pilot, "up")                          # Balanced -> Economy
            assert "Economy" in body_text(app).split(">")[1][:20]
            await press(pilot, "down")
            await press(pilot, "down")                        # -> Higher usage
            assert "2 GB/day x 10 travel day(s) = 20 GB" in body_text(app)
            await press(pilot, "enter")
            await press(pilot, "enter")
            await press(pilot, "y")
        assert app.result.trip.daily_data_gb == DATA_PRESETS[2][1]
        assert app.result.trip.legs[0].required_gb == 20.0
    run(scenario())


def test_choice_wraps_and_does_not_need_the_mouse():
    async def scenario():
        app = WizardApp(make_geo(), WizardState(), None, False)
        async with app.run_test(size=SIZE) as pilot:
            await pilot.pause()
            await add_leg(pilot, "spain", "1")
            await press(pilot, "enter")
            await answer_with(pilot, "EUR")
            await answer_with(pilot, "")
            await press(pilot, "up")                          # Balanced -> Economy
            await press(pilot, "up")                          # wraps to the last option
            assert app.data_sel == len(DATA_PRESETS) - 1
            await press(pilot, "up")
            await press(pilot, "up")
            assert app.data_sel == len(DATA_PRESETS) - 3
    run(scenario())


# --------------------------------------------------------------------------
# text/numeric questions and validation
# --------------------------------------------------------------------------
def test_bad_currency_keeps_you_on_the_question_with_your_text():
    async def scenario():
        app = WizardApp(make_geo(), WizardState(), None, False)
        async with app.run_test(size=SIZE) as pilot:
            await pilot.pause()
            await add_leg(pilot, "spain", "4")
            await press(pilot, "enter")
            await answer_with(pilot, "15")
            assert "currency" in status_text(app).lower()
            assert "Supported" in body_text(app)
            assert answer(app).value == "15"                  # preserved for fixing
            assert app.pref_step == 0
            for _ in range(2):
                await press(pilot, "backspace")               # ordinary editing works
            await type_text(pilot, "GBP")
            await press(pilot, "enter")
            assert app.pref_step == 1 and app.state.currency == "GBP"
    run(scenario())


def test_days_must_be_a_whole_number_and_the_answer_is_kept():
    async def scenario():
        app = WizardApp(make_geo(), WizardState(), None, False)
        async with app.run_test(size=SIZE) as pilot:
            await pilot.pause()
            await press(pilot, "a")
            await answer_with(pilot, "spain")
            await answer_with(pilot, "four")
            assert "not a whole number" in status_text(app)
            assert answer(app).value == "four"              # kept so it can be fixed
            assert app.form_pos == 1                        # still on the days field
            await press(pilot, "backspace", "backspace", "backspace", "backspace")
            await answer_with(pilot, "0")
            assert "at least 1" in status_text(app)
            await press(pilot, "backspace")
            await type_text(pilot, "5")
            await press(pilot, "enter")
            await answer_with(pilot, "0")
        assert [(leg.country.name, leg.days) for leg in app.state.legs] == [("Spain", 5)]
    run(scenario())


def test_unknown_destination_is_reported_and_the_text_is_kept():
    async def scenario():
        app = WizardApp(make_geo(), WizardState(), None, False)
        async with app.run_test(size=SIZE) as pilot:
            await pilot.pause()
            await press(pilot, "a")
            await answer_with(pilot, "atlantis")
            assert "no country matches" in status_text(app)
            assert answer(app).value == "atlantis"
            assert app.state.legs == []
    run(scenario())


def test_blank_budget_means_no_cap():
    async def scenario():
        app = WizardApp(make_geo(), WizardState(), None, False)
        async with app.run_test(size=SIZE) as pilot:
            await pilot.pause()
            await add_leg(pilot, "spain", "4")
            await press(pilot, "enter")
            await answer_with(pilot, "EUR")
            await press(pilot, "enter")                       # blank budget
            assert app.state.budget is None
            assert app.pref_step == 2
            assert app.mode == "choice"
            # The data-level screen asks for one thing: no leftover "no budget cap"
            # echo duplicating what the Review line will say anyway.
            assert status_text(app) == ""
    run(scenario())


# --------------------------------------------------------------------------
# ordinary text editing is never treated as a command
# --------------------------------------------------------------------------
def test_letters_that_are_commands_stay_text_while_typing():
    """"denmark" must not delete a leg and "e" must not start an edit mid-word."""
    async def scenario():
        app = WizardApp(make_geo(), WizardState(), None, False)
        async with app.run_test(size=SIZE) as pilot:
            await pilot.pause()
            await add_leg(pilot, "spain", "4")
            before = len(app.state.legs)
            await press(pilot, "a")                            # add: destination field
            await type_text(pilot, "de")                       # d and e are commands elsewhere
            assert answer(app).value == "de"
            assert len(app.state.legs) == before               # nothing deleted/edited
            assert app.mode == "form"
            await type_text(pilot, "s")                        # "des" -> resolves to a country?
            await press(pilot, "backspace")                    # ordinary Backspace editing
            assert answer(app).value == "de"
            await press(pilot, "escape")                       # abandon this entry
            assert len(app.state.legs) == before
            assert app.mode == "list"
    run(scenario())


def test_arrow_keys_edit_text_instead_of_moving_the_list_cursor():
    async def scenario():
        app = WizardApp(make_geo(), WizardState(), None, False)
        async with app.run_test(size=SIZE) as pilot:
            await pilot.pause()
            await add_leg(pilot, "spain", "4")
            await add_leg(pilot, "france", "7")
            assert app.sel == 1
            await press(pilot, "a")
            await type_text(pilot, "spain")
            await press(pilot, "left", "left")             # caret moves, not the list cursor
            assert app.sel == 1
            await press(pilot, "backspace")                # deletes before the caret
            assert answer(app).value == "spin"
            await type_text(pilot, "a")                        # re-inserted mid-word
            assert answer(app).value == "spain"
            await press(pilot, "enter")
            assert app.form_country is not None and app.form_country.name == "Spain"
    run(scenario())


# --------------------------------------------------------------------------
# add / edit / delete / undo, all by keyboard
# --------------------------------------------------------------------------
def test_edit_the_selected_leg_with_e():
    async def scenario():
        app = WizardApp(make_geo(), WizardState(), None, False)
        async with app.run_test(size=SIZE) as pilot:
            await pilot.pause()
            await add_leg(pilot, "spain", "4")
            await add_leg(pilot, "france", "7")
            await press(pilot, "up")                           # select Spain
            assert app.sel == 0
            assert ">1" in body_text(app) or " >1" in body_text(app)
            await press(pilot, "e")
            assert app.mode == "form" and app.form_kind == "edit"
            assert answer(app).value == "4"                    # prefilled
            await press(pilot, "backspace")
            await type_text(pilot, "9")
            await press(pilot, "enter")
            await answer_with(pilot, "1")                      # transfer days
        assert [(leg.country.name, leg.days, leg.transfer_days) for leg in app.state.legs] == [
            ("Spain", 9, 1), ("France", 7, 0)]
    run(scenario())


def test_delete_asks_for_confirmation_and_u_undoes_it():
    async def scenario():
        app = WizardApp(make_geo(), WizardState(), None, False)
        async with app.run_test(size=SIZE) as pilot:
            await pilot.pause()
            await add_leg(pilot, "spain", "4")
            await add_leg(pilot, "france", "7")
            await press(pilot, "up")                           # Spain
            await press(pilot, "d")
            assert app.mode == "yesno"
            assert "Delete leg 1" in body_text(app)
            assert "Enter yes" in hint_text(app)          # y still works, just not shown
            assert "y yes" not in hint_text(app)
            await press(pilot, "n")                            # decline
            assert len(app.state.legs) == 2 and app.mode == "list"
            await press(pilot, "d")
            await press(pilot, "enter")                        # Enter = yes here
            assert len(app.state.legs) == 1
            assert "u to undo" in status_text(app)
            await press(pilot, "u")                            # undo
            assert [leg.country.name for leg in app.state.legs] == ["Spain", "France"]
            assert app.state.legs[0].days == 4                 # back in its old position
            await press(pilot, "u")                            # nothing left to undo
            assert "nothing to undo" in status_text(app)
    run(scenario())


def test_transfer_marker_and_its_confirmation_are_keyboard_only():
    async def scenario():
        app = WizardApp(make_geo(), WizardState(), None, False)
        async with app.run_test(size=SIZE) as pilot:
            await pilot.pause()
            await add_leg(pilot, "spain", "4")
            await press(pilot, "t")                            # transfer marker
            assert app.mode == "form" and app.form_kind == "transfer"
            await answer_with(pilot, "2")
            await add_leg(pilot, "france", "3")
        legs = app.state.legs
        assert [leg.country.name if leg.country else "(transfer)" for leg in legs] == [
            "Spain", "(transfer)", "France"]
        assert legs[1].transfer_days == 2 and legs[1].days == 0
    run(scenario())


def test_edit_cannot_be_used_on_a_transfer_marker():
    async def scenario():
        app = WizardApp(make_geo(), WizardState(), None, False)
        async with app.run_test(size=SIZE) as pilot:
            await pilot.pause()
            await press(pilot, "t")
            await answer_with(pilot, "1")
            await press(pilot, "e")
            assert "transfer marker" in status_text(app)
            assert app.mode == "list"
    run(scenario())


# --------------------------------------------------------------------------
# back navigation preserves answers
# --------------------------------------------------------------------------
def test_alt_left_goes_back_and_keeps_every_answer():
    async def scenario():
        app = WizardApp(make_geo(), WizardState(), None, False)
        async with app.run_test(size=SIZE) as pilot:
            await pilot.pause()
            await add_leg(pilot, "spain", "4")
            await add_leg(pilot, "france", "7", "2")
            await press(pilot, "enter")
            await answer_with(pilot, "GBP")
            await answer_with(pilot, "200")
            await press(pilot, "down")                         # Higher usage selected
            await press(pilot, "alt+left")                     # back to the itinerary
            assert app.stage == 0 and app.mode == "list"
            assert [(leg.country.name, leg.days, leg.transfer_days) for leg in app.state.legs] == [
                ("Spain", 4, 0), ("France", 7, 2)]
            assert app.state.currency == "GBP" and app.state.budget == 200.0
            await press(pilot, "enter")                        # forward again
            assert app.pref_step == 0 and answer(app).value == "GBP"
            await press(pilot, "enter")
            assert app.pref_step == 1 and answer(app).value == "200"
            await press(pilot, "enter")
            assert app.pref_step == 2
            assert app.data_sel == 2                           # the earlier choice kept
    run(scenario())


def test_alt_left_from_review_returns_to_the_data_choice():
    async def scenario():
        app = WizardApp(make_geo(), WizardState(), None, False)
        async with app.run_test(size=SIZE) as pilot:
            await pilot.pause()
            await to_review(pilot, data_steps=1)
            assert app.stage == 2
            await press(pilot, "alt+left")
            assert app.stage == 1 and app.pref_step == 2 and app.mode == "choice"
            await press(pilot, "enter")
            assert app.stage == 2
    run(scenario())


def test_escape_during_an_add_keeps_the_previous_legs():
    async def scenario():
        app = WizardApp(make_geo(), WizardState(), None, False)
        async with app.run_test(size=SIZE) as pilot:
            await pilot.pause()
            await add_leg(pilot, "spain", "4")
            await press(pilot, "a")
            await answer_with(pilot, "france")
            await answer_with(pilot, "7")
            await press(pilot, "escape")                       # before the last field
            assert len(app.state.legs) == 1
            assert app.mode == "list"
            assert "unchanged" in status_text(app)
    run(scenario())


def test_escape_arms_then_cancels_at_the_first_stage():
    async def scenario():
        app = WizardApp(make_geo(), WizardState(), None, False)
        async with app.run_test(size=SIZE) as pilot:
            await pilot.pause()
            await press(pilot, "escape")                       # first press: warn only
            assert app.result is None and app._esc_armed
            assert "Esc again" in status_text(app)
            await press(pilot, "escape")                       # second press: cancel
            await pilot.pause()
        assert app.result.cancelled and app.result.trip is None
    run(scenario())


# --------------------------------------------------------------------------
# searching, saving, cancellation
# --------------------------------------------------------------------------
def test_review_searches_without_asking_again():
    """One Enter on Review is the search - there is no second prompt."""
    async def scenario():
        app = WizardApp(make_geo(), WizardState(), None, False)
        async with app.run_test(size=SIZE) as pilot:
            await pilot.pause()
            await to_review(pilot)
            await press(pilot, "enter")                        # searches directly
            await pilot.pause()
        assert app.result is not None and not app.result.cancelled
        assert app.result.trip is not None
    run(scenario())


def test_saves_the_itinerary_from_the_review_stage(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    async def scenario():
        app = WizardApp(make_geo(), WizardState(), None, False)
        async with app.run_test(size=SIZE) as pilot:
            await pilot.pause()
            await to_review(pilot)
            await press(pilot, "s")
            assert app.mode == "text"
            await press(pilot, "escape")                       # not yet
            assert app.mode == "list"
            await press(pilot, "s")
            await answer_with(pilot, "trip.json")
            assert "saved" in status_text(app)
            await press(pilot, "enter")
            await press(pilot, "enter")
        doc = json.loads((tmp_path / "trip.json").read_text())
        assert [s["country"] for s in doc["stops"]] == ["Spain"]
        assert doc["currency"] == "EUR"
        assert app.result.save_path.resolve() == tmp_path / "trip.json"
    run(scenario())


def test_an_existing_file_is_not_overwritten_on_enter(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "itinerary.json").write_text("ORIGINAL")

    async def scenario():
        app = WizardApp(make_geo(), WizardState(), None, False)
        async with app.run_test(size=SIZE) as pilot:
            await pilot.pause()
            await to_review(pilot)
            await press(pilot, "s")
            await press(pilot, "enter")                        # default path
            assert app.mode == "yesno"
            await press(pilot, "enter")                        # Enter must NOT overwrite
            assert "keep" in status_text(app) or "not saved" in status_text(app)
            assert (tmp_path / "itinerary.json").read_text() == "ORIGINAL"
            await press(pilot, "s")
            await press(pilot, "enter")
            await press(pilot, "y")                            # explicit y overwrites
            assert "saved" in status_text(app)
        assert json.loads((tmp_path / "itinerary.json").read_text())["currency"] == "EUR"
    run(scenario())


def test_ctrl_c_cancels_cleanly():
    async def scenario():
        app = WizardApp(make_geo(), WizardState(), None, False)
        async with app.run_test(size=SIZE) as pilot:
            await pilot.pause()
            await add_leg(pilot, "spain", "4")
            await press(pilot, "ctrl+c")
            await pilot.pause()
        assert app.result is not None and app.result.cancelled
        assert app.result.trip is None
    run(scenario())


def test_ctrl_c_cancels_while_a_field_is_open():
    async def scenario():
        app = WizardApp(make_geo(), WizardState(), None, False)
        async with app.run_test(size=SIZE) as pilot:
            await pilot.pause()
            await press(pilot, "a")
            await type_text(pilot, "spain")
            await press(pilot, "ctrl+c")
            await pilot.pause()
        assert app.result.cancelled
    run(scenario())


def test_an_exception_inside_the_app_still_returns_a_result():
    """A crash goes through Textual's crash hook, which must still produce a result."""
    async def scenario():
        app = WizardApp(make_geo(), WizardState(), None, False)
        try:
            async with app.run_test(size=SIZE) as pilot:
                await pilot.pause()
                # refresh_screen runs for every action, so this crashes the next key.
                app.refresh_screen = lambda: (_ for _ in ()).throw(RuntimeError("boom"))
                await press(pilot, "a")
                await pilot.pause(0.2)
        except Exception:
            pass     # Textual may surface the crash to the caller; either way...
        # ...the hook ran first, so the caller gets a clean cancellation, not a hang.
        assert app.result is not None and app.result.cancelled
        assert app.result.trip is None
        assert app._crash_error == "boom"
    run(scenario())


def test_run_tui_wizard_turns_a_keyboard_interrupt_into_a_cancellation(monkeypatch):
    def boom(self, *a, **k):
        raise KeyboardInterrupt

    monkeypatch.setattr(WizardApp, "run", boom)
    result = run_tui_wizard(make_geo(), WizardState(), None, False)
    assert result.cancelled and result.trip is None


# --------------------------------------------------------------------------
# resumed itineraries
# --------------------------------------------------------------------------
def test_a_resumed_itinerary_opens_on_the_list_with_the_legs_selected(tmp_path):
    from esimdb_checker.itinerary import save_itinerary_file
    from esimdb_checker.wizard import state_from_file

    path = tmp_path / "trip.json"
    state = WizardState()
    state.currency, state.budget, state.daily_data_gb = "USD", None, 1.5
    geo = make_geo()
    state.add_destination(geo.resolve_country("Spain"), 4, 0)
    state.add_destination(geo.resolve_country("France"), 7, 0)
    save_itinerary_file(state.build_trip(), path, overwrite=True)

    async def scenario():
        resumed = state_from_file(path, geo)
        app = WizardApp(geo, resumed, path, False)
        async with app.run_test(size=SIZE) as pilot:
            await pilot.pause()
            assert app.mode == "list"
            assert "Spain" in body_text(app) and "France" in body_text(app)
            assert "Itinerary - legs are searched in this order" in body_text(app)
            await press(pilot, "down")
            await press(pilot, "e")
            assert answer(app).value == "7"                    # France prefilled
            await press(pilot, "backspace")
            await type_text(pilot, "9")
            await press(pilot, "enter")
            await press(pilot, "enter")                        # transfer unchanged
            await press(pilot, "enter")                        # continue
            await press(pilot, "enter")                        # currency kept
            await press(pilot, "enter")                        # budget kept
            await press(pilot, "enter")                        # data kept
            await press(pilot, "enter")                        # search
            await press(pilot, "y")
        trip = app.result.trip
        assert [(leg.country.name, leg.days) for leg in trip.legs] == [
            ("Spain", 4), ("France", 9)]
        assert trip.currency == "USD"
    run(scenario())


def test_hint_and_status_text_are_literal_never_rich_markup():
    """Bracketed key names must render as text, not be eaten as markup tags."""
    async def scenario():
        app = WizardApp(make_geo(), WizardState(), None, False)
        async with app.run_test(size=SIZE) as pilot:
            await pilot.pause()
            app._status("press [search] then Esc twice")
            app._hint("Alt+Left back  ·  [Y]/n")
            await pilot.pause()
            assert "[search]" in status_text(app)
            assert "[Y]/n" in hint_text(app)
    run(scenario())


# --------------------------------------------------------------------------
# screens that ask nothing show no answer box and repeat nothing
# --------------------------------------------------------------------------
def test_no_answer_box_where_nothing_is_being_typed():
    """The list, data-level and Review screens are information only."""
    async def scenario():
        app = WizardApp(make_geo(), WizardState(), None, False)
        async with app.run_test(size=SIZE) as pilot:
            await pilot.pause()
            assert answer(app).display is False          # itinerary list
            await add_leg(pilot, "spain", "4")
            await press(pilot, "a")
            assert answer(app).display is True           # a question is open
            await press(pilot, "escape")
            assert answer(app).display is False          # back to the list
            await press(pilot, "enter")
            assert answer(app).display is True           # currency
            await answer_with(pilot, "EUR")
            assert answer(app).display is True           # budget
            await answer_with(pilot, "200")
            assert app.mode == "choice"
            assert answer(app).display is False          # data-level choice
            await press(pilot, "enter")
            assert app.mode == "list" and app.stage == 2
            assert answer(app).display is False          # review
            assert answer(app).placeholder == ""         # no stale question text
    run(scenario())


def test_the_chosen_values_are_still_shown_once_on_review():
    """Removing the status echo must not hide the values: Review states them."""
    async def scenario():
        app = WizardApp(make_geo(), WizardState(), None, False)
        async with app.run_test(size=SIZE) as pilot:
            await pilot.pause()
            await to_review(pilot, legs=(("spain", "4"),), currency="GBP",
                            budget="15", data_steps=1)
            assert app.stage == 2
            assert status_text(app) == ""
            review = body_text(app)
            assert "GBP" in review and "15" in review and "2 GB/day" in review
    run(scenario())


def test_esc_is_the_back_key_on_review():
    """Esc leaves Review for the previous screen - it does not cancel the wizard."""
    async def scenario():
        app = WizardApp(make_geo(), WizardState(), None, False)
        async with app.run_test(size=SIZE) as pilot:
            await pilot.pause()
            await to_review(pilot)
            await press(pilot, "escape")
            assert app.result is None                    # still running
            assert app.stage == 1 and app.mode == "choice"
            # ...and Alt+Left still works, even though the hint no longer advertises it
            await press(pilot, "enter")
            assert app.stage == 2
            await press(pilot, "alt+left")
            assert app.stage == 1 and app.result is None
    run(scenario())


# --------------------------------------------------------------------------
# the search runs inside the interface, not after the terminal comes back
# --------------------------------------------------------------------------
def test_confirming_the_search_keeps_the_screen_live():
    """No drop back to the shell while esimdb.com is being queried."""
    import threading
    gate = threading.Event()
    seen: list = []

    def fake_search(trip, progress):
        seen.append(trip)
        progress(1, 2, "Spain")
        gate.wait(5)
        progress(2, 2, "France")
        return "REPORT"

    async def scenario():
        app = WizardApp(make_geo(), WizardState(), None, False, search=fake_search)
        async with app.run_test(size=SIZE) as pilot:
            await pilot.pause()
            await to_review(pilot)
            await press(pilot, "enter")                       # Enter searches directly
            assert app.mode == "busy", "Review must not stop at a confirmation page"
            await pilot.pause(0.05)
            assert app.result is None, "the interview exited before the search finished"
            body = body_text(app)
            assert "Searching esimdb.com" in body, body
            assert "Spain (1 of 2)" in body, body            # real per-destination progress
            await press(pilot, "a")                           # cannot disturb a live query
            assert app.mode == "busy"
            gate.set()
            for _ in range(60):
                await pilot.pause(0.05)
                if app.result is not None:
                    break
            assert app.result is not None
            assert app.result.report == "REPORT"
            assert app.result.trip is not None and not app.result.cancelled
        assert len(seen) == 1                                  # exactly one query
    run(scenario())


def test_a_failing_search_is_a_result_not_a_crash():
    """A network failure during the in-interface search is reported, not raised."""
    def boom(trip, progress):
        raise RuntimeError("esimdb.com unreachable")

    async def scenario():
        app = WizardApp(make_geo(), WizardState(), None, False, search=boom)
        async with app.run_test(size=SIZE) as pilot:
            await pilot.pause()
            await to_review(pilot)
            await press(pilot, "enter")
            for _ in range(60):
                await pilot.pause(0.05)
                if app.result is not None:
                    break
            assert app.result is not None
            assert app.result.search_error == "esimdb.com unreachable"
            assert app.result.report is None
            assert app.result.trip is not None
    run(scenario())


# --------------------------------------------------------------------------
# Review is a read-only summary: no cursor, no leg commands, no extra page
# --------------------------------------------------------------------------
def test_review_shows_no_cursor_and_no_redundant_prompt_line():
    async def scenario():
        app = WizardApp(make_geo(), WizardState(), None, False)
        async with app.run_test(size=SIZE) as pilot:
            await pilot.pause()
            await to_review(pilot, legs=(("spain", "4"), ("france", "7")))
            review = body_text(app)
            assert ">" not in review                       # nothing to select
            assert "Press Enter to search" not in review   # the hint line says it
            assert "currency EUR" in review
    run(scenario())


def test_review_ignores_the_navigation_and_editing_keys():
    """Up/down/a/e/d/u/t do nothing on Review - the legs were settled earlier."""
    async def scenario():
        app = WizardApp(make_geo(), WizardState(), None, False)
        async with app.run_test(size=SIZE) as pilot:
            await pilot.pause()
            await to_review(pilot, legs=(("spain", "4"), ("france", "7")))
            before = [leg.label for leg in app.state.legs]
            for key in ("up", "down", "up", "a", "e", "d", "u", "t"):
                await press(pilot, key)
                assert app.mode == "list" and app.stage == 2, key
                assert [leg.label for leg in app.state.legs] == before, key
                assert ">" not in body_text(app), key
            assert app.result is None                       # none of them searched
    run(scenario())


def test_the_review_hint_lists_only_what_review_can_do():
    async def scenario():
        app = WizardApp(make_geo(), WizardState(), None, False)
        async with app.run_test(size=SIZE) as pilot:
            await pilot.pause()
            await to_review(pilot)
            hint = hint_text(app)
            assert hint == "s save  Enter search  Esc back", hint
            for gone in ("a add", "e edit", "d delete", "u undo", "select"):
                assert gone not in hint, hint
    run(scenario())
