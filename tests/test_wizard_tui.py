"""Headless tests for the in-place Textual wizard (App.run_test / Pilot).

These drive the real widgets and keyboard bindings without a terminal: the app
runs on Textual's headless driver, so alternate-screen setup/teardown and cursor
restore are exercised by Textual itself.
"""

from __future__ import annotations

import asyncio
import json

from conftest import make_geo
from textual.widgets import Button, Input, Static

from esimdb_checker.wizard import WizardApp, WizardState, run_tui_wizard


def run(coro):
    asyncio.run(coro)


def set_field(app, widget_id: str, value: str) -> None:
    app.query_one(f"#{widget_id}", Input).value = value


def body_text(app) -> str:
    return app.query_one("#body", Static).render().plain


def status_text(app) -> str:
    return app.query_one("#status", Static).render().plain


def progress_text(app) -> str:
    return app.query_one("#progress", Static).render().plain


def field_text(app, widget_id: str) -> str:
    return app.query_one(f"#{widget_id}", Input).value


async def click(pilot, selector: str) -> None:
    """Click a button, bracketed by pauses that bracket Textual's press animation.

    Textual ignores a mouse click on a button that is still playing its 0.2s press
    animation (which a keyboard Enter on a focused button also triggers), so the
    helper settles before and after the click - as a real user's timing would.
    """
    await pilot.pause(0.25)
    assert await pilot.click(selector), f"{selector} was not under the pointer"
    await pilot.pause(0.3)


async def add_leg(pilot, app, dest, days, transfer="0"):
    set_field(app, "value", dest)
    set_field(app, "value2", days)
    set_field(app, "value3", transfer)
    await click(pilot, "#btn_action")


async def to_preferences(pilot, app):
    await click(pilot, "#btn_next")


async def answer(pilot, app, text, button="#btn_action"):
    """Type into the active field and press the stage's action button."""
    set_field(app, "value", text)
    await click(pilot, button)


async def confirm_enter(pilot):
    """Confirm the current stage with the keyboard (Enter)."""
    await pilot.press("enter")
    await pilot.pause(0.3)


async def build(app, pilot, legs=(("spain", "4", "0"),), currency="", budget=""):
    """Drive a fresh session through destinations + preferences to the review stage."""
    for dest, days, transfer in legs:
        await add_leg(pilot, app, dest, days, transfer)
    await to_preferences(pilot, app)
    await answer(pilot, app, currency or "EUR")      # currency
    await answer(pilot, app, budget)                  # budget ("" = none)
    await confirm_enter(pilot)                        # data level (selected)


# ---------------------------------------------------------------------------
def test_named_stages_are_shown_as_progress():
    async def scenario():
        app = WizardApp(make_geo(), WizardState(), None, False)
        async with app.run_test(size=(110, 30)) as pilot:
            await pilot.pause(0.05)
            prog = progress_text(app)
            for name in ("Destinations", "Preferences", "Review"):
                assert name in prog
            assert "Review" in prog
    run(scenario())


def test_add_two_legs_and_search():
    async def scenario():
        app = WizardApp(make_geo(), WizardState(), None, False)
        async with app.run_test(size=(110, 30)) as pilot:
            await pilot.pause(0.05)
            await build(app, pilot, (("spain", "4", "0"), ("france", "7", "0")))
            assert app.stage == 2, status_text(app)
            assert "Spain" in body_text(app) and "France" in body_text(app)
            await click(pilot, "#btn_action")            # search this itinerary
        assert app.result is not None and not app.result.cancelled
        trip = app.result.trip
        assert [(leg.country.name, leg.days) for leg in trip.legs] == [("Spain", 4), ("France", 7)]
        assert (trip.legs[1].start_day, trip.legs[1].end_day) == (5, 11)
        assert trip.currency == "EUR" and trip.total_days == 11
        assert trip.budget is None
    run(scenario())


def test_transfer_days_are_per_leg_and_not_charged():
    async def scenario():
        app = WizardApp(make_geo(), WizardState(), None, False)
        async with app.run_test(size=(110, 30)) as pilot:
            await pilot.pause(0.05)
            await build(app, pilot, (("spain", "4", "3"), ("france", "5", "2")))
            await click(pilot, "#btn_action")
            await pilot.pause(0.05)
        trip = app.result.trip
        assert [leg.transfer_days for leg in trip.legs] == [3, 2]
        assert trip.total_days == 9
        assert abs(trip.required_gb - 13.5) < 1e-6
    run(scenario())


def test_invalid_currency_stays_in_the_preferences_stage():
    async def scenario():
        app = WizardApp(make_geo(), WizardState(), None, False)
        async with app.run_test(size=(110, 30)) as pilot:
            await pilot.pause(0.05)
            await add_leg(pilot, app, "spain", "4")
            await to_preferences(pilot, app)
            assert app.stage == 1 and app.pref_step == 0

            await answer(pilot, app, "15")             # the old "15" defect
            assert app.stage == 1 and app.pref_step == 0, "must not advance on bad input"
            assert app.state.currency is None
            msg = status_text(app)
            assert msg.startswith("error:") and "looks like a number" in msg

            await answer(pilot, app, "XYZ")
            assert app.pref_step == 0
            assert "'XYZ' is not a supported currency" in status_text(app)

            await answer(pilot, app, "gbp")            # valid, lowercase
            assert app.pref_step == 1
            assert app.state.currency == "GBP"
    run(scenario())


def test_budget_step_accepts_blank_as_no_budget():
    async def scenario():
        app = WizardApp(make_geo(), WizardState(), None, False)
        async with app.run_test(size=(110, 30)) as pilot:
            await pilot.pause(0.05)
            await add_leg(pilot, app, "spain", "4")
            await to_preferences(pilot, app)
            await answer(pilot, app, "USD")
            await answer(pilot, app, "")               # no budget
            assert app.pref_step == 2
            assert app.state.budget is None and app.state.currency == "USD"
    run(scenario())


def test_data_level_selector_higher_usage_is_two_gb():
    async def scenario():
        app = WizardApp(make_geo(), WizardState(), None, False)
        async with app.run_test(size=(110, 30)) as pilot:
            await pilot.pause(0.05)
            await add_leg(pilot, app, "spain", "34")   # 34 charged days
            await to_preferences(pilot, app)
            await answer(pilot, app, "EUR")
            await answer(pilot, app, "")
            assert app.pref_step == 2
            assert "1.5 GB/day" in body_text(app)      # Balanced pre-selected
            assert "51 GB" in body_text(app)           # stated for this trip

            await pilot.press("down")                  # -> Higher usage
            await pilot.pause(0.05)
            assert app.data_sel == 2
            assert "2 GB/day" in body_text(app) and "68 GB" in body_text(app)

            await pilot.press("enter")
            await pilot.pause(0.05)
            await click(pilot, "#btn_action")
            await pilot.pause(0.05)
        trip = app.result.trip
        assert trip.daily_data_gb == 2.0
        assert trip.total_days == 34
        assert abs(trip.required_gb - 68.0) < 1e-9
    run(scenario())


def test_arrow_keys_cycle_the_selector_without_leaving_the_stage():
    async def scenario():
        app = WizardApp(make_geo(), WizardState(), None, False)
        async with app.run_test(size=(110, 30)) as pilot:
            await pilot.pause(0.05)
            await add_leg(pilot, app, "spain", "4")
            await to_preferences(pilot, app)
            await answer(pilot, app, "EUR")
            await answer(pilot, app, "")
            await pilot.press("up")                    # Balanced -> Economy (wraps)
            await pilot.pause(0.05)
            assert app.data_sel == 0
            assert app.pref_step == 2 and app.stage == 1
            assert "1 GB/day" in body_text(app)
    run(scenario())


def test_back_preserves_every_entered_value():
    async def scenario():
        app = WizardApp(make_geo(), WizardState(), None, False)
        async with app.run_test(size=(110, 30)) as pilot:
            await pilot.pause(0.05)
            await build(app, pilot, (("spain", "4", "0"),), currency="USD", budget="250")
            assert app.stage == 2

            await click(pilot, "#btn_back")             # Review -> data level
            await pilot.pause(0.05)
            assert app.stage == 1 and app.pref_step == 2
            assert app.data_sel == 1

            await click(pilot, "#btn_back")             # -> budget (value kept)
            await pilot.pause(0.05)
            assert app.pref_step == 1
            assert field_text(app, "value") == "250"

            await click(pilot, "#btn_back")             # -> currency (value kept)
            await pilot.pause(0.05)
            assert app.pref_step == 0
            assert field_text(app, "value") == "USD"

            await click(pilot, "#btn_back")             # -> destinations (legs kept)
            await pilot.pause(0.05)
            assert app.stage == 0
            assert [leg.days for leg in app.state.legs] == [4]
    run(scenario())


def test_edit_and_remove_a_leg():
    async def scenario():
        app = WizardApp(make_geo(), WizardState(), None, False)
        async with app.run_test(size=(110, 30)) as pilot:
            await pilot.pause(0.05)
            await add_leg(pilot, app, "spain", "4")
            await add_leg(pilot, app, "france", "7")

            await click(pilot, "#btn_edit")             # choose the leg
            await pilot.pause(0.05)
            set_field(app, "value", "2")
            await click(pilot, "#btn_edit")             # load it for editing
            await pilot.pause(0.05)
            assert field_text(app, "value") == "France"
            set_field(app, "value2", "10")
            set_field(app, "value3", "1")
            await click(pilot, "#btn_action")           # save the edit
            await pilot.pause(0.05)
            assert [(leg.days, leg.transfer_days) for leg in app.state.legs] == [(4, 0), (10, 1)]

            set_field(app, "value", "1")
            await click(pilot, "#btn_remove")          # enter remove mode
            await click(pilot, "#btn_remove")          # ... and act on the typed leg number
            await pilot.pause(0.05)
            assert [leg.country.name for leg in app.state.legs] == ["France"]
    run(scenario())


def test_screen_is_replaced_not_appended():
    async def scenario():
        app = WizardApp(make_geo(), WizardState(), None, False)
        async with app.run_test(size=(110, 30)) as pilot:
            await pilot.pause(0.05)
            for dest in ("spain", "france", "japan"):
                await add_leg(pilot, app, dest, "3")
            text = body_text(app)
            assert text.count("TOTAL:") == 1
            assert text.count("Add a leg to your itinerary:") == 1
            for dest in ("Spain", "France", "Japan"):
                assert text.count(dest) == 1, text
    run(scenario())


def test_unknown_destination_is_reported_inside_the_stage():
    async def scenario():
        app = WizardApp(make_geo(), WizardState(), None, False)
        async with app.run_test(size=(110, 30)) as pilot:
            await pilot.pause(0.05)
            await add_leg(pilot, app, "atlantis", "4")
            assert app.state.legs == []
            msg = status_text(app)
            assert msg.startswith("error:") and "no country matches" in msg
            assert app.stage == 0
    run(scenario())


def test_missing_days_are_reported_and_the_stage_stays():
    async def scenario():
        app = WizardApp(make_geo(), WizardState(), None, False)
        async with app.run_test(size=(110, 30)) as pilot:
            await pilot.pause(0.05)
            set_field(app, "value", "spain")
            set_field(app, "value2", "four")
            await click(pilot, "#btn_action")
            await pilot.pause(0.05)
            assert app.state.legs == []
            assert "not a whole number" in status_text(app)

            set_field(app, "value2", "4")
            set_field(app, "value3", "0")
            await click(pilot, "#btn_action")
            await pilot.pause(0.05)
            assert [leg.days for leg in app.state.legs] == [4]
    run(scenario())


def test_keyboard_only_path_uses_enter_on_the_focused_button():
    async def scenario():
        app = WizardApp(make_geo(), WizardState(), None, False)
        async with app.run_test(size=(110, 30)) as pilot:
            await pilot.pause(0.05)
            set_field(app, "value", "spain")
            await pilot.press("enter")                 # still missing days
            await pilot.pause(0.05)
            assert app.state.legs == [] and "whole number" in status_text(app)

            app.query_one("#value2", Input).focus()
            await pilot.press("4")                     # keystrokes go to the focused field
            await pilot.pause(0.05)
            await pilot.press("enter")                 # action with an empty transfer field
            await pilot.pause(0.05)
            assert app.state.legs == [] and "transfer days" in status_text(app)

            app.query_one("#value3", Input).focus()
            await pilot.press("0")
            await pilot.press("enter")                 # focused button -> press()
            await pilot.pause(0.05)
            assert [(leg.days, leg.transfer_days) for leg in app.state.legs] == [(4, 0)]

            await pilot.press("tab")                   # focus "add leg" ...
            app.query_one("#btn_next", Button).focus()
            await pilot.press("enter")                 # ... and advance with Enter
            await pilot.pause(0.05)
            assert app.stage == 1
    run(scenario())


def test_escape_cancels_without_a_result():
    async def scenario():
        app = WizardApp(make_geo(), WizardState(), None, False)
        async with app.run_test(size=(110, 30)) as pilot:
            await pilot.pause(0.05)
            await add_leg(pilot, app, "spain", "4")
            await pilot.press("escape")
            await pilot.pause(0.05)
        assert app.result is not None and app.result.cancelled
        assert app.result.trip is None
    run(scenario())


def test_cancel_button_cancels():
    async def scenario():
        app = WizardApp(make_geo(), WizardState(), None, False)
        async with app.run_test(size=(110, 30)) as pilot:
            await pilot.pause(0.05)
            await click(pilot, "#btn_cancel")
            await pilot.pause(0.05)
        assert app.result.cancelled and app.result.trip is None
    run(scenario())


def test_review_can_save_the_itinerary_to_a_file(tmp_path):
    target = tmp_path / "itinerary.json"

    async def scenario():
        app = WizardApp(make_geo(), WizardState(), None, False)
        async with app.run_test(size=(110, 30)) as pilot:
            await pilot.pause(0.05)
            await build(app, pilot, (("spain", "4", "2"),), currency="GBP")
            set_field(app, "value", str(target))
            await click(pilot, "#btn_save")
            await pilot.pause(0.05)
            assert "saved itinerary to" in status_text(app)
            await click(pilot, "#btn_action")           # search
            await pilot.pause(0.05)
        assert app.result.save_path == target
        doc = json.loads(target.read_text())
        assert [s["country"] for s in doc["stops"]] == ["Spain"]
        assert doc["currency"] == "GBP" and doc["stops"][0]["transfer_days"] == 2
    run(scenario())


def test_existing_file_is_not_overwritten_without_confirmation(tmp_path):
    target = tmp_path / "itinerary.json"
    target.write_text('{"keep": "me"}', encoding="utf-8")

    async def scenario():
        app = WizardApp(make_geo(), WizardState(), save_path=target, overwrite=False)
        async with app.run_test(size=(110, 30)) as pilot:
            await pilot.pause(0.05)
            await build(app, pilot, (("spain", "4", "0"),))
            set_field(app, "value", str(target))
            await click(pilot, "#btn_save")
            await pilot.pause(0.05)
            assert "already exists" in status_text(app)
            assert json.loads(target.read_text()) == {"keep": "me"}
            assert app.saved_path is None
    run(scenario())


def test_incomplete_itinerary_cannot_reach_review():
    async def scenario():
        app = WizardApp(make_geo(), WizardState(), None, False)
        async with app.run_test(size=(110, 30)) as pilot:
            await pilot.pause(0.05)
            await click(pilot, "#btn_next")
            await pilot.pause(0.05)
            assert app.stage == 0                      # refused: no legs yet
            assert "at least one destination" in status_text(app)
    run(scenario())


# ---------------------------------------------------------------------------
# Terminal safety: interrupts and unexpected failures must never escape to the
# user's shell, and must leave a usable terminal (cancelled result).
# ---------------------------------------------------------------------------
def test_keyboard_interrupt_does_not_escape():
    """Ctrl+C must end the TUI cleanly and report a cancellation, never traceback."""
    def boom(self):
        raise KeyboardInterrupt

    original = WizardApp.run
    WizardApp.run = boom
    try:
        result = run_tui_wizard(make_geo(), WizardState(), None, False)
    finally:
        WizardApp.run = original
    assert result.cancelled and result.trip is None


def test_run_tui_wizard_without_a_result_object_reports_cancellation(monkeypatch):
    """If the app somehow exits without setting a result, treat it as cancelled."""
    monkeypatch.setattr(WizardApp, "run", lambda self: None)
    result = run_tui_wizard(make_geo(), WizardState(), None, False)
    assert result.cancelled and result.trip is None


# --------------------------------------------------------------------------
# Regressions found by driving the app in a real pseudo-terminal (see README
# "Verified in a terminal"): Rich ate the literal "[button name]" hints, and the
# three input boxes had no width rule, so only the first was visible.
# --------------------------------------------------------------------------
def test_button_names_in_status_and_help_survive_rich_markup():
    """Literal [add leg] style hints must render, not be swallowed as markup tags."""
    async def scenario():
        app = WizardApp(make_geo(), WizardState(), None, False)
        async with app.run_test(size=(110, 34)) as pilot:
            await pilot.pause()
            status = str(app.query_one("#status", Static).render().plain)
            help_text = str(app.query_one("#help", Static).render().plain)
            assert "[add leg]" in status, status
            assert "[edit]/[remove]" in help_text, help_text

            # and again on the review stage, where the hints name other buttons
            set_field(app, "value", "spain")
            set_field(app, "value2", "4")
            set_field(app, "value3", "0")
            await click(pilot, "#btn_action")
            await pilot.pause()
            await click(pilot, "#btn_next")
            await pilot.pause()
            assert "[use currency ->]" in str(app.query_one("#status", Static).render().plain)
    run(scenario())


def test_the_three_input_boxes_are_visible_side_by_side():
    """Each Input needs an explicit width, or every one takes the whole row."""
    async def scenario():
        app = WizardApp(make_geo(), WizardState(), None, False)
        async with app.run_test(size=(110, 34)) as pilot:
            await pilot.pause()
            app.query_one("#value2", Input).disabled = False
            app.query_one("#value3", Input).disabled = False
            await pilot.pause()
            names = ("value", "value2", "value3")
            regions = [app.query_one(f"#{i}", Input).region for i in names]
            for wid, r in zip(names, regions, strict=True):
                assert r.width > 8, f"{wid} is only {r.width} columns wide"
            assert regions[0].x < regions[1].x < regions[2].x, regions
            assert regions[0].y == regions[1].y == regions[2].y, regions
            # no overlap: each box ends before the next begins
            assert regions[0].x + regions[0].width <= regions[1].x, regions
            assert regions[1].x + regions[1].width <= regions[2].x, regions
    run(scenario())


def test_yes_in_the_review_save_field_uses_the_default_name(tmp_path, monkeypatch):
    """"y" is a yes, not a filename - it must not create a file called "y"."""
    monkeypatch.chdir(tmp_path)

    async def scenario():
        app = WizardApp(make_geo(), WizardState(), None, False)
        async with app.run_test(size=(110, 30)) as pilot:
            await pilot.pause(0.05)
            await build(app, pilot, (("spain", "4", "0"),), currency="GBP")
            set_field(app, "value", "y")
            await click(pilot, "#btn_save")
            await pilot.pause(0.05)
            assert "saved itinerary to" in status_text(app)
            await click(pilot, "#btn_action")           # search this itinerary
            await pilot.pause(0.05)
        assert app.result.save_path.resolve() == tmp_path / "itinerary.json"
        assert (tmp_path / "itinerary.json").exists()
        assert not (tmp_path / "y").exists()
    run(scenario())
