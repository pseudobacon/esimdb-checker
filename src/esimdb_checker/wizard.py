"""Interactive itinerary wizard.

One shared state machine (`WizardState`) is driven by two interchangeable
front-ends:

  * a compact in-place Textual TUI — a keyboard-only alternate-screen app that
    refreshes only its own area, restores the terminal on exit, and keeps
    validation errors in the current stage. It has no buttons and never captures
    the mouse;
  * a plain line-oriented fallback (``--plain``, or when stdout is redirected /
    not a TTY).

Both front-ends use the SAME validation and the SAME ``build_trip`` /
``save_itinerary_file`` logic, so itinerary construction, currency validation,
and recommendation are never duplicated. Country resolution always uses the
shared ``Geo`` resolver.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import geo as geo_mod
from .fx import supported_currency_codes, validate_currency_code
from .itinerary import StopError, build_trip, load_itinerary_file, save_itinerary_file
from .models import Country, Trip

# ---------------------------------------------------------------------------
# Defaults and presets (single source of truth, shared by both front-ends)
# ---------------------------------------------------------------------------
DEFAULT_CURRENCY = "EUR"
DEFAULT_DAILY_DATA = 1.5

# (label, GB/day) — the three offered presets. Entering a literal number of
# GB/day is also accepted in plain mode; the TUI uses an arrow-key selector so
# numeric and GB/day inputs are never mixed.
DATA_PRESETS: list[tuple[str, float]] = [
    ("Economy", 1.0),
    ("Balanced", 1.5),
    ("Higher usage", 2.0),
]

STAGES: list[str] = ["Destinations", "Preferences", "Review"]


@dataclass
class WizardResult:
    trip: Trip | None
    cancelled: bool
    save_path: Path | None = None
    # Set when the TUI ran the search itself, so the terminal stays in the interface
    # while esimdb.com is being queried. `search_error` is the failure to report.
    report: Any | None = None
    search_error: str | None = None


# ---------------------------------------------------------------------------
# Shared state machine
# ---------------------------------------------------------------------------
@dataclass
class LegEntry:
    country: Country | None
    days: int = 0
    transfer_days: int = 0

    @property
    def is_transfer(self) -> bool:
        return self.country is None

    @property
    def label(self) -> str:
        if self.is_transfer:
            return f"(transfer) {self.transfer_days} transfer day(s), not charged"
        s = f"{self.country.name}: {self.days} day(s)"
        if self.transfer_days:
            s += f" (+{self.transfer_days} transfer)"
        return s


@dataclass
class WizardState:
    """The itinerary being assembled. Both UIs read/write only this object."""

    legs: list[LegEntry] = field(default_factory=list)
    currency: str | None = None  # normalized supported code
    budget: float | None = None  # None = no budget cap
    daily_data_gb: float | None = None

    # ---- legs ----------------------------------------------------------
    def charged(self) -> list[LegEntry]:
        return [leg for leg in self.legs if not leg.is_transfer and leg.days > 0]

    def add_destination(self, country: Country, days: int, transfer: int = 0) -> None:
        self.legs.append(LegEntry(country=country, days=days, transfer_days=transfer))

    def add_transfer(self, transfer_days: int) -> None:
        self.legs.append(LegEntry(country=None, days=0, transfer_days=transfer_days))

    def edit_leg(self, idx: int, days: int, transfer: int) -> None:
        leg = self.legs[idx]
        if leg.country is not None:
            leg.days = days
            leg.transfer_days = transfer

    def remove_leg(self, idx: int) -> None:
        del self.legs[idx]

    def insert_leg(self, idx: int, leg: LegEntry) -> None:
        """Re-insert a removed leg at its old position (undo support)."""
        self.legs.insert(min(max(idx, 0), len(self.legs)), leg)

    # ---- preferences (each returns (ok, error_or_none)) -----------------
    def set_currency(self, raw) -> tuple[bool, str | None]:
        code, err = validate_currency_code(raw)
        if err is not None:
            return False, err
        self.currency = code
        return True, None

    def set_budget(self, raw) -> tuple[bool, str | None]:
        v = str(raw).strip()
        if v.lower() in ("", "n", "no", "none", "-"):
            self.budget = None
            return True, None
        try:
            f = float(v)
        except ValueError:
            return False, "budget must be a number (or leave blank for no budget)"
        if f <= 0:
            return False, "budget must be a positive number"
        self.budget = f
        return True, None

    def set_daily_data(self, raw) -> tuple[bool, str | None]:
        v = str(raw).strip().lower()
        for name, gb in DATA_PRESETS:
            if v in (name.lower(), name.split()[0].lower()):
                self.daily_data_gb = gb
                return True, None
        try:
            f = float(v)
        except ValueError:
            return False, (
                "enter a number of GB/day (or one of: "
                + ", ".join(n for n, _ in DATA_PRESETS)
                + ")"
            )
        if f <= 0:
            return False, "GB/day must be a positive number"
        self.daily_data_gb = f
        return True, None

    def daily_data_for(self) -> float:
        return self.daily_data_gb if self.daily_data_gb is not None else DEFAULT_DAILY_DATA

    # ---- build / summary -------------------------------------------------
    def can_build(self) -> tuple[bool, str]:
        if not self.legs:
            return False, "no legs yet — add at least one destination"
        if not self.charged():
            return False, "need at least one destination leg with days > 0"
        if not self.currency:
            return False, "currency not set"
        if self.daily_data_gb is None:
            return False, "daily data not set"
        return True, ""

    def build_trip(self) -> Trip:
        ok, err = self.can_build()
        if not ok:
            raise StopError(err)
        stops = [(leg.country, leg.days, leg.transfer_days) for leg in self.legs]
        return build_trip(stops, daily_data_gb=self.daily_data_gb,
                          currency=self.currency, budget=self.budget)

    def summary_lines(self, show_data: bool = True) -> list[str]:
        """Compact leg table with travel-day spans (mirrors the report's days).

        ``show_data`` is False during the destinations stage, before the daily
        data level has been chosen, so no GB figures are invented from a default.
        """
        daily = self.daily_data_for()
        lines: list[str] = []
        cursor = 1
        for i, leg in enumerate(self.legs, start=1):
            cursor += leg.transfer_days
            start = cursor
            end = cursor + leg.days - 1 if leg.days > 0 else cursor - 1
            if leg.is_transfer:
                span = f"(transfer {leg.transfer_days}d)" if leg.transfer_days else "(transfer)"
                lines.append(f"  {i}. (transfer)             {span}")
            else:
                span_txt = f"days {start:>2}-{end:<2}"
                if show_data:
                    span_txt += f"   {leg.days * daily:g} GB"
                lines.append(f"  {i}. {leg.country.name:<20} {leg.days:>3} day(s)   {span_txt}")
            if leg.days > 0:
                cursor = end + 1
        total = sum(leg.days for leg in self.legs)
        if show_data:
            lines.append(
                f"  TOTAL: {total} charged day(s), {total * daily:g} GB @ {daily:g} GB/day"
            )
        else:
            lines.append(f"  TOTAL: {total} charged day(s)")
        return lines

    def review_lines(self) -> list[str]:
        ok, _ = self.can_build()
        lines = self.summary_lines()
        lines.append(f"  currency: {self.currency or '(unset)'}")
        budget = ("no budget cap" if self.budget is None
                  else f"budget: {self.budget:g} {self.currency}")
        lines.append(f"  {budget}")
        if not ok:
            lines.append("  (incomplete — see validation above)")
        return lines


# ---------------------------------------------------------------------------
# Loading a saved itinerary into a fresh state (for editing / reuse)
# ---------------------------------------------------------------------------
def state_from_file(path: Path, geo) -> WizardState:
    """Load a saved itinerary file into a new WizardState (destinations resolved)."""
    stops, settings = load_itinerary_file(path)  # validates currency, daily, budget
    state = WizardState(currency=settings["currency"], daily_data_gb=settings["daily_data_gb"],
                        budget=settings.get("budget"))
    for name, days, transfer in stops:
        if name is None:
            state.add_transfer(transfer)
            continue
        c = geo.resolve_country(name)
        if c is None:
            cands = geo.candidates(name)
            if len(cands) != 1:
                raise geo_mod.CountryNotFoundError(
                    f"could not resolve {name!r} from saved itinerary")
            c = cands[0]
        state.add_destination(c, days, transfer)
    return state


# ---------------------------------------------------------------------------
# Plain line-oriented front-end (injected I/O so it is fully offline-testable)
# ---------------------------------------------------------------------------
def _resolve(geo, token: str):
    c = geo.resolve_country(token)
    if c is None:
        cands = geo.candidates(token)
        if len(cands) == 1:
            return cands[0], None
        if len(cands) > 1:
            return None, "more than one match; be more specific: " + ", ".join(
                f"{c.name} ({c.code})" for c in cands
            )
        return None, f"no country matches {token!r}"
    return c, None


def _plain_destinations(state: WizardState, geo, ask, out) -> None:
    # A resumed itinerary already has legs, so open with the menu rather than
    # "enter a destination to start".
    first = not state.legs
    while True:
        if state.legs:
            out("")
            out("Itinerary so far:")
            out("\n".join(state.summary_lines(show_data=False)))
        if first:
            token = _ask(ask, out, "Destination (country name, ISO code, or slug)")
        else:
            token = _ask(
                ask, out,
                "Add [d]estination, [t]ransfer, [e]dit, [r]emove — or press Enter to finish\n"
                "  (or just type a country, e.g. Thailand — menu words are whole words)",
            )
        low = token.strip().lower()
        # Menu words are matched EXACTLY. Matching on the first letter would turn
        # ordinary country names into commands: "thailand" -> [t]ransfer,
        # "estonia" -> [e]dit, "romania" -> [r]emove.
        if low in FINISH_WORDS:
            if first:
                out("  enter a destination to start (name, ISO code, or slug).")
                continue
            if state.charged():
                return
            out("  add at least one destination leg with days > 0 before continuing.")
            continue
        if low in EDIT_WORDS:
            idx = _ask_leg_index(state, ask, out, "Which leg to edit?")
            if idx is not None:
                leg = state.legs[idx]
                if leg.country is not None:
                    d = _ask_int_loop(ask, out, f"Days in {leg.country.name}", leg.days, 1)
                    t = _ask_int_loop(ask, out, "Transfer days before this leg (0 = none)",
                                      leg.transfer_days, 0)
                    state.edit_leg(idx, d, t)
                    out(f"  updated leg {idx + 1}: {state.legs[idx].label}")
                else:
                    out("  transfer legs cannot be edited — remove it and add a new one.")
            first = False
            continue
        if low in REMOVE_WORDS:
            idx = _ask_leg_index(state, ask, out, "Which leg to remove?")
            if idx is not None:
                out(f"  removed: {state.legs[idx].label}")
                state.remove_leg(idx)
            first = False
            continue
        if low in TRANSFER_WORDS:
            t = _ask_int_loop(ask, out, "Transfer days (0 = uncharged marker only)", 0, 0)
            state.add_transfer(t)
            out(f"  added transfer leg ({t} day(s), not charged)")
            first = False
            continue
        if low in ADD_WORDS:
            # explicit "add a destination": ask for the name instead of guessing one
            token = _ask(ask, out, "Destination (country name, ISO code, or slug)")
            low = token.strip().lower()
        # Anything else is treated as a destination name/code/slug.
        c, err = _resolve(geo, token)
        if err is not None:
            out(f"  {err}")
            first = False
            continue
        d = _ask_int_loop(ask, out, f"Days in {c.name}", None, 1)
        t = _ask_int_loop(ask, out, "Transfer/connectivity days before this leg (0 = none)", 0, 0)
        state.add_destination(c, d, t)
        out(f"  added: {c.name}: {d} day(s)" + (f" (+{t} transfer, not charged)" if t else ""))
        first = False


def _plain_preferences(state: WizardState, ask, out) -> None:
    out("")
    out("Preferences")
    out(f"  supported currencies: {', '.join(supported_currency_codes())}")
    while True:
        cur = _ask(ask, out, "Currency for prices and budget", state.currency or DEFAULT_CURRENCY)
        ok, err = state.set_currency(cur)
        if ok:
            out(f"  currency: {state.currency}")
            break
        out(f"  {err}")
    while True:
        b = _ask(ask, out, f"Total trip budget in {state.currency} (blank for none)", "")
        ok, err = state.set_budget(b)
        if ok:
            budget_txt = "none" if state.budget is None else f"{state.budget:g} {state.currency}"
            out(f"  budget: {budget_txt}")
            break
        out(f"  {err}")
    out("")
    out("  Expected data use per destination day:")
    out("    1   GB/day   — Economy")
    out("    1.5 GB/day   — Balanced")
    out("    2   GB/day   — Higher usage")
    while True:
        v = _ask(ask, out, "GB per day (or Economy/Balanced/Higher)", _daily_default_str(state))
        ok, err = state.set_daily_data(v)
        if ok:
            total = sum(leg.days for leg in state.legs)
            out(f"  using {state.daily_data_gb:g} GB/day "
                f"-> {total} day trip uses {total * state.daily_data_gb:g} GB")
            break
        out(f"  {err}")


def _daily_default_str(state: WizardState) -> str:
    gb = state.daily_data_gb if state.daily_data_gb is not None else DEFAULT_DAILY_DATA
    return f"{gb:g}"


def _plain_review(state: WizardState, ask, out, save_path, overwrite) -> Path | None:
    out("")
    out("Confirm your itinerary:")
    out("\n".join(state.review_lines()))
    if not _ask_yes_no(ask, out, "Search this itinerary?", default_yes=True):
        raise StopError("cancelled at the confirmation prompt (nothing searched)")
    return _plain_save(state, save_path, overwrite, ask, out)


def _plain_save(state, save_path, overwrite, ask, out) -> Path | None:
    if save_path is None:
        p = _ask_optional(ask, out,
                          "Save this itinerary to a file? (enter a path, y for "
                          "itinerary.json, or Enter to skip)")
        answer = (p or "").strip()
        if not answer or answer.lower() in ("n", "no"):
            return None
        # A bare yes/no answer must never create a file literally named "y" or "n".
        save_path = Path("itinerary.json" if answer.lower() in ("y", "yes") else answer)
    try:
        saved = save_itinerary_file(state.build_trip(), save_path, overwrite=overwrite)
        out(f"  Saved itinerary to {saved}")
        return saved
    except StopError as e:
        if "already exists" in str(e):
            if _ask_yes_no(ask, out, f"  {save_path} already exists — overwrite?",
                           default_yes=False, tolerant=True):
                saved = save_itinerary_file(state.build_trip(), save_path, overwrite=True)
                out(f"  Overwrote itinerary at {saved}")
                return saved
            out("  Not saved.")
            return None
        out(f"  Could not save: {e}")
        return None
    except OSError as e:
        out(f"  Could not save: {e}")
        return None


def run_plain_wizard(
    geo,
    state: WizardState,
    ask: Callable[[], str],
    out: Callable[[str], None],
    save_path: Path | None = None,
    overwrite: bool = False,
) -> WizardResult:
    out("Itinerary wizard (plain mode)")
    out("  (type 'q' at a prompt to abort)")
    try:
        _plain_destinations(state, geo, ask, out)
        _plain_preferences(state, ask, out)
        saved = _plain_review(state, ask, out, save_path, overwrite)
        trip = state.build_trip()
        return WizardResult(trip=trip, cancelled=False, save_path=saved)
    except StopError as e:
        out(f"  wizard aborted: {e}")
        return WizardResult(trip=None, cancelled=True, save_path=None)


# ---------------------------------------------------------------------------
# Plain front-end menu words
# ---------------------------------------------------------------------------
# Matched exactly (case-insensitively) - never by first letter. First-letter
# matching silently turned country names into commands: "thailand" -> [t]ransfer,
# "estonia" -> [e]dit, "romania"/"russia" -> [r]emove, "denmark" -> [d]estination.
# Anything that is not one of these words is a destination name/code/slug.
FINISH_WORDS = frozenset({"", "n", "no", "done", "finish", "next", "ok"})
ADD_WORDS = frozenset({"d", "add", "dest", "destination", "new", "leg"})
TRANSFER_WORDS = frozenset({"t", "tr", "transfer", "transit"})
EDIT_WORDS = frozenset({"e", "ed", "edit", "change", "modify"})
REMOVE_WORDS = frozenset({"r", "rm", "remove", "delete", "del"})


# ---------------------------------------------------------------------------
# Small prompt helpers (plain front-end)
# ---------------------------------------------------------------------------
def _ask(ask, out, prompt: str, default: str = "") -> str:
    shown = f"{prompt} [{default}]" if default not in ("", None) else f"{prompt}:"
    out(shown)
    try:
        v = ask()
    except EOFError as exc:
        raise StopError("no more input available (EOF)") from exc
    v = (v or "").strip()
    if v.lower() == "q":
        raise StopError("aborted by user")
    return v if v != "" else default


def _ask_optional(ask, out, prompt: str, default: str = "") -> str:
    """Like _ask, but input exhaustion simply takes the default.

    Used for genuinely optional questions (e.g. saving), so a closed stdin at the
    very last prompt never discards an itinerary the user already confirmed.
    """
    try:
        return _ask(ask, out, prompt, default)
    except StopError:
        return default


def _ask_int(ask, out, prompt: str, default, minimum: int):
    v = _ask(ask, out, prompt, str(default) if default is not None else "")
    try:
        n = int(v)
    except ValueError:
        out(f"  '{v}' is not a whole number; try again")
        return None
    if n < minimum:
        out(f"  must be at least {minimum}; try again")
        return None
    return n


def _ask_int_loop(ask, out, prompt: str, default, minimum: int) -> int:
    """Keep re-asking (in the same stage) until a valid integer is given."""
    while True:
        n = _ask_int(ask, out, prompt, default, minimum)
        if n is not None:
            return n


def _ask_leg_index(state: WizardState, ask, out, prompt: str) -> int | None:
    count = len(state.legs)
    if count == 0:
        out("  (no legs to choose from)")
        return None
    while True:
        v = _ask(ask, out, f"{prompt} [1-{count}]", "")
        try:
            n = int(v)
        except ValueError:
            out(f"  '{v}' is not a number; try again")
            continue
        if 1 <= n <= count:
            return n - 1
        out(f"  out of range (1-{count}); try again")


def _ask_yes_no(ask, out, prompt: str, default_yes: bool, tolerant: bool = False) -> bool:
    default = "y" if default_yes else "n"
    try:
        v = _ask(ask, out, prompt, default)
    except StopError:
        if tolerant:
            return default_yes
        raise
    v = v.lower()
    if v.startswith("y"):
        return True
    if v.startswith("n"):
        return False
    return default_yes


# ---------------------------------------------------------------------------
# Textual TUI front-end (alternate screen, in-place refresh, KEYBOARD ONLY)
# ---------------------------------------------------------------------------
# There are deliberately no Button widgets and no mouse capture (see
# run_tui_wizard's mouse=False): every action is a key press, and the keys that
# work in the current context are always on screen in the hint line.
try:
    from textual.app import App, ComposeResult
    from textual.binding import Binding
    from textual.widgets import Input, Static

    _TEXTUAL = True
except Exception:  # pragma: no cover - Textual missing
    App = ComposeResult = Binding = Input = Static = object  # type: ignore[misc,assignment]
    _TEXTUAL = False


if _TEXTUAL:
    from rich.text import Text

    # Compact, non-clickable keyboard hints. One line per context. Esc is the back
    # key everywhere; Alt+Left still works but is not worth spending hint space on.
    HINT_LIST = ("\u2191/\u2193 select  a add  e edit  d delete  u undo  t transfer  "
                 "Enter continue  Esc cancel")
    # Review is read-only: the legs were settled in the previous stage, so there is
    # nothing to move or edit there - only search, save, or go back.
    HINT_LIST_REVIEW = "s save  Enter search  Esc back"
    HINT_FORM = "type the answer  Enter next  Esc cancel this entry"
    HINT_TEXT = "type the answer  Enter submit  Esc back"
    HINT_CHOICE = "\u2191/\u2193 choose  Enter select  Esc back"
    HINT_YESNO = "Enter yes  n no  Esc no"

    class WizardApp(App):
        """Keyboard-only itinerary wizard on the alternate screen.

        Questionnaire model: text/numeric questions are typed into one input and
        Enter validates and advances; choice questions are picked with Up/Down and
        Enter; the itinerary list is moved through with Up/Down and edited with
        a/e/d (plus u to undo a delete). Backspace is never a navigation key, so
        ordinary text editing is untouched.
        """

        CSS = """
        Screen { background: $background; }
        #header { text-style: bold; }
        #progress { padding: 0 1 0 1; }
        #body { padding: 0 1; height: auto; }
        #answer { padding: 0 1; height: auto; }
        #status { padding: 0 1; min-height: 1; }
        #hint { padding: 0 1; text-style: dim; }
        Input { width: 1fr; }
        """

        BINDINGS = [
            Binding("ctrl+c", "cancel", "cancel the wizard", priority=True, show=False),
        ]

        def __init__(self, geo, state: WizardState, save_path: Path | None,
                     overwrite: bool, search: Callable | None = None):
            super().__init__()
            self.geo = geo
            self.state = state
            self.save_path = save_path
            self.overwrite = overwrite
            # search(trip, progress) -> Report. When present the interview does not end
            # at "search?": the query runs here so the terminal stays in the interface.
            self.search = search
            self.stage = 0                 # 0 itinerary - 1 preferences - 2 review
            self.pref_step = 0             # 0 currency - 1 budget - 2 data level
            self.mode = "list"             # list | form | text | choice | yesno | busy
            self.sel = 0                   # selected leg index
            self.form_kind = "add"         # add | edit | transfer
            self.form_fields: tuple[str, ...] = ()
            self.form_pos = 0
            self.form_draft: dict[str, str] = {}
            self.form_country: Country | None = None
            self.form_edit_idx: int | None = None
            self.text_kind = ""            # currency | budget | save
            self.yesno_kind = ""           # delete | overwrite
            self.pending_delete: int | None = None
            self.pending_overwrite: str | None = None
            self.undo_slot: tuple[int, LegEntry] | None = None
            self.data_sel = 1              # default selection: Balanced
            self.saved_path: Path | None = None
            self.result: WizardResult | None = None
            self._err_text = ""
            self._crash_error: str = ""
            self._esc_armed = False
            self._trip: Trip | None = None      # the trip being searched
            self._report: Any | None = None
            self._search_error: str = ""
            self._search_note = ""                 # e.g. "Thailand (3 of 5)"
            self._elapsed = 0.0
            self._spinner = 0
            self._ticker = None

        # -- widgets -------------------------------------------------------
        def compose(self) -> ComposeResult:
            yield Static("eSIM itinerary wizard (keyboard only)", id="header")
            yield Static("", id="progress")
            yield Static("", id="body")
            yield Input(id="answer", placeholder="answer", disabled=True)
            yield Static("", id="status")
            yield Static("", id="hint")

        def on_mount(self) -> None:
            self._answer.can_focus = False      # only an open field may take focus
            self.refresh_screen()

        def _st(self, id: str) -> Static:
            return self.query_one(f"#{id}", Static)

        @property
        def _answer(self) -> Input:
            return self.query_one("#answer", Input)

        def _open_input(self, value: str = "", placeholder: str = "") -> None:
            inp = self._answer
            inp.display = True
            inp.disabled = False
            inp.can_focus = True
            inp.placeholder = placeholder
            # Only assign when it differs: setting .value while a burst of keystrokes
            # is still being dispatched wipes the characters the user just typed.
            if inp.value != value:
                inp.value = value
            inp.focus()

        def _close_input(self) -> None:
            """Hide the answer field: screens that ask nothing show nothing.

            Keeping the field on screen "just in case" put an empty box carrying the
            previous question's placeholder ("budget amount") on the Review and
            data-level screens, next to a status line echoing the last answer — all of
            which the body above already states. Keys that arrive in the same batch as
            the batch that opens a field are applied in `on_key`, so hiding it here
            still costs nothing typed.
            """
            inp = self._answer
            inp.value = ""
            inp.placeholder = ""
            inp.can_focus = False
            inp.disabled = True
            inp.display = False
            inp.blur()

        def _typing(self) -> bool:
            """True while a text/numeric answer is open for typing."""
            return not self._answer.disabled

        def _status(self, text: str, err: bool = False) -> None:
            self._err_text = text if err else ""
            # A Text object (not a str) keeps hints like "Alt+Left" or "[Y]/n"
            # literal: Static would otherwise eat bracketed text as Rich markup.
            if err:
                self._st("status").update(
                    Text.assemble(("error: ", "bold red"), (text, "red"))
                )
            else:
                self._st("status").update(Text(text))

        def _err(self, text: str) -> None:
            self._status(text, err=True)

        def _ok(self, text: str) -> None:
            self._status(text, err=False)

        def _hint(self, text: str) -> None:
            self._st("hint").update(Text(text))

        def _progress_text(self) -> str:
            return "   ".join(
                ("\u25cf " if i <= self.stage else "\u25cb ") + name
                for i, name in enumerate(STAGES)
            )

        # -- rendering -----------------------------------------------------
        def refresh_screen(self) -> None:
            self._st("progress").update(Text(self._progress_text()))
            if self.mode == "form":
                self._render_form()
            elif self.mode == "text":
                self._render_text()
            elif self.mode == "choice":
                self._render_choice()
            elif self.mode == "yesno":
                self._render_yesno()
            elif self.mode == "busy":
                self._render_busy()
            else:
                self._render_list()

        def _list_lines(self, show_data: bool, cursor: bool = True) -> list[str]:
            """Leg table, optionally with the keyboard cursor (not on Review)."""
            lines = self.state.summary_lines(show_data=show_data)
            out: list[str] = []
            for i, line in enumerate(lines):
                if cursor and i < len(self.state.legs):
                    line = (" >" if i == self.sel else "  ") + line[2:]
                else:
                    line = "  " + line[2:]
                out.append(line)
            return out

        def _render_list(self) -> None:
            s = self.state
            self._close_input()
            if not s.legs:
                head = ["No legs yet.", "", "Press a to add a destination "
                        "(days are asked per destination)."]
            else:
                head = [
                    ("Itinerary - legs are searched in this order" if self.stage == 0
                     else "Review - the itinerary that will be searched"),
                    "",
                ] + self._list_lines(show_data=self.stage == 2,
                                     cursor=self.stage == 0) + [""]
                if self.stage == 2:
                    budget = ("no budget cap" if s.budget is None
                              else f"budget {s.budget:g} {s.currency}")
                    head.append(
                        f"currency {s.currency}  -  {budget}  -  "
                        f"{s.daily_data_for():g} GB/day")
                else:
                    head.append("Press Enter to continue." if s.charged()
                                else "Add at least one destination leg first (press a).")
            self._st("body").update(Text("\n".join(head)))
            self._hint(HINT_LIST if self.stage == 0 else HINT_LIST_REVIEW)

        def _form_intro(self) -> str:
            if self.form_kind == "transfer":
                return "Add an uncharged transfer marker (it shifts later travel days)."
            if self.form_kind == "edit":
                leg = self.state.legs[self.form_edit_idx or 0]
                return f"Edit leg {self.form_edit_idx + 1}: {leg.label}"
            return "Add a leg."

        def _form_question(self, field: str) -> tuple[str, str]:
            name = self.form_country.name if self.form_country else ""
            if self.form_kind == "transfer":
                return ("Transfer days for this marker (0 = marker only)", "0")
            if field == "dest":
                return ("Destination (name, ISO code, or slug)", "e.g. Spain, ES, spain")
            if field == "days":
                if self.form_kind == "edit":
                    return (f"Days in {name} (whole number, 1 or more)", "days")
                return (f"Days in {name} (whole number, 1 or more)", "days")
            return ("Transfer/connectivity days before this leg (0 = none)", "0")

        def _render_form(self) -> None:
            field = self.form_fields[self.form_pos]
            q, ph = self._form_question(field)
            step = f"({self.form_pos + 1} of {len(self.form_fields)})"
            self._st("body").update(Text(f"{self._form_intro()}  {step}\n\n{q}"))
            self._open_input(self.form_draft.get(field, ""), ph)
            self._hint(HINT_FORM)

        def _text_question(self) -> tuple[str, str, str]:
            s = self.state
            if self.text_kind == "currency":
                body = ("Currency for prices, budget, and totals\n"
                        "Supported: " + ", ".join(supported_currency_codes()))
                return body, s.currency or DEFAULT_CURRENCY, "currency (e.g. EUR)"
            if self.text_kind == "budget":
                now = ("no budget cap" if s.budget is None
                       else f"{s.budget:g} {s.currency}")
                return (f"Total trip budget in {s.currency} (blank = no budget)\n"
                        f"Current: {now}",
                        "" if s.budget is None else f"{s.budget:g}",
                        "budget amount")
            default = str(self.save_path) if self.save_path else "itinerary.json"
            return (f"Save this itinerary to a file (Enter = {default})",
                    "", f"path (default {default})")

        def _render_text(self) -> None:
            body, value, ph = self._text_question()
            self._st("body").update(Text(body))
            self._open_input(value, ph)
            self._hint(HINT_TEXT)

        def _render_choice(self) -> None:
            s = self.state
            self._close_input()
            total = sum(leg.days for leg in s.legs)
            lines = ["Expected data per destination day", ""]
            for i, (name, gb) in enumerate(DATA_PRESETS):
                marker = ">" if i == self.data_sel else " "
                lines.append(f"  {marker} {name:<12} {gb:g} GB/day")
            cur = DATA_PRESETS[self.data_sel][1]
            lines.append("")
            lines.append(f"  {cur:g} GB/day x {total} travel day(s) = "
                         f"{total * cur:g} GB")
            self._st("body").update(Text("\n".join(lines)))
            self._hint(HINT_CHOICE)

        def _render_yesno(self) -> None:
            s = self.state
            self._close_input()
            if self.yesno_kind == "delete":
                idx = self.pending_delete if self.pending_delete is not None else self.sel
                label = s.legs[idx].label if idx < len(s.legs) else "?"
                body = ("\n".join(self._list_lines(show_data=self.stage == 2))
                        + f"\n\nDelete leg {idx + 1}: {label} ?")
                self._st("body").update(Text(body))
            elif self.yesno_kind == "overwrite":
                self._st("body").update(Text(
                    f"{self.pending_overwrite} already exists.\n\n"
                    "Overwrite it? (y = yes; Enter or n keeps the existing file)"))
            # Overwriting a file is the one question where Enter is NOT "yes".
            self._hint(HINT_YESNO if self.yesno_kind != "overwrite"
                       else "y overwrite  n or Enter keep the existing file  Esc keep it")

        # -- stage plumbing ------------------------------------------------
        def _to_stage(self, stage: int) -> None:
            self.stage = stage
            self.sel = min(self.sel, max(0, len(self.state.legs) - 1))
            if stage == 1:
                self.mode = "choice" if self.pref_step == 2 else "text"
                self.text_kind = {0: "currency", 1: "budget"}.get(self.pref_step, "")
            else:
                self.mode = "list"
                self.text_kind = ""
            self.refresh_screen()

        # -- list / editing ------------------------------------------------
        def _move(self, delta: int) -> None:
            if self.state.legs:
                self.sel = (self.sel + delta) % len(self.state.legs)
                self.refresh_screen()

        def _continue(self) -> None:
            """Enter on the itinerary stage: go on to the preferences."""
            if not self.state.charged():
                return self._err("add at least one destination leg with days > 0")
            self.pref_step = 0
            self._to_stage(1)
        def _start_add(self) -> None:
            self.form_kind = "add"
            self.form_fields = ("dest", "days", "transfer")
            self.form_pos = 0
            self.form_draft = {"dest": "", "days": "", "transfer": ""}
            self.form_country = None
            self.form_edit_idx = None
            self.mode = "form"
            self.refresh_screen()

        def _start_transfer(self) -> None:
            self.form_kind = "transfer"
            self.form_fields = ("transfer",)
            self.form_pos = 0
            self.form_draft = {"transfer": "0"}
            self.form_country = None
            self.form_edit_idx = None
            self.mode = "form"
            self.refresh_screen()

        def _start_edit(self) -> None:
            if not self.state.legs:
                return self._err("no leg to edit - press a to add one")
            leg = self.state.legs[self.sel]
            if leg.is_transfer:
                return self._err("a transfer marker has no days to edit - press d to delete it")
            self.form_kind = "edit"
            self.form_fields = ("days", "transfer")
            self.form_pos = 0
            self.form_draft = {"days": str(leg.days), "transfer": str(leg.transfer_days)}
            self.form_country = leg.country
            self.form_edit_idx = self.sel
            self.mode = "form"
            self.refresh_screen()

        def _parse_int(self, raw: str, what: str, minimum: int) -> int | None:
            if raw == "":
                self._err(f"enter a whole number for {what}")
                return None
            if not raw.lstrip("-").isdigit():
                self._err(f"'{raw}' is not a whole number")
                return None
            n = int(raw)
            if n < minimum:
                self._err(f"{what} must be at least {minimum}")
                return None
            return n

        def _form_submit(self) -> None:
            field = self.form_fields[self.form_pos]
            raw = self._answer.value.strip()
            if field == "dest":
                c, err = _resolve(self.geo, raw)
                if c is None:
                    return self._err(err)          # keeps what they typed
                self.form_country = c
                self.form_draft["dest"] = c.name
            else:
                minimum = 1 if field == "days" else 0
                n = self._parse_int(raw, "days" if field == "days" else "transfer days",
                                    minimum)
                if n is None:
                    return                          # keeps what they typed
                self.form_draft[field] = str(n)
            self.form_pos += 1
            if self.form_pos < len(self.form_fields):
                self.refresh_screen()
                return
            self._commit_form()

        def _commit_form(self) -> None:
            s = self.state
            if self.form_kind == "transfer":
                t = int(self.form_draft["transfer"])
                s.add_transfer(t)
                self.sel = len(s.legs) - 1
                self._ok(f"added transfer leg ({t} day(s), not charged)")
            elif self.form_kind == "add":
                c = self.form_country
                d = int(self.form_draft["days"])
                t = int(self.form_draft["transfer"])
                s.add_destination(c, d, t)          # type: ignore[arg-type]
                self.sel = len(s.legs) - 1
                extra = f" (+{t} transfer)" if t else ""
                self._ok(f"added {c.name}: {d} day(s){extra}")  # type: ignore[union-attr]
            else:
                idx = self.form_edit_idx or 0
                d = int(self.form_draft["days"])
                t = int(self.form_draft["transfer"])
                s.edit_leg(idx, d, t)
                self.sel = idx
                self._ok(f"updated leg {idx + 1}: {s.legs[idx].label}")
            self.form_kind = "add"
            self.form_edit_idx = None
            self.mode = "list"
            self.refresh_screen()

        def _ask_delete(self) -> None:
            if not self.state.legs:
                return self._err("no leg to delete")
            self.pending_delete = self.sel
            self.yesno_kind = "delete"
            self.mode = "yesno"
            self.refresh_screen()

        def _undo_delete(self) -> None:
            if self.undo_slot is None:
                self._ok("nothing to undo")
                return
            idx, leg = self.undo_slot
            self.state.insert_leg(idx, leg)
            self.undo_slot = None
            self.sel = min(idx, len(self.state.legs) - 1)
            self._ok(f"restored leg {idx + 1}: {leg.label}")
            self.refresh_screen()

        def _answer_yesno(self, yes: bool) -> None:
            kind, self.yesno_kind = self.yesno_kind, ""
            if kind == "delete":
                idx = self.pending_delete
                self.pending_delete = None
                if yes and idx is not None and idx < len(self.state.legs):
                    leg = self.state.legs[idx]
                    self.state.remove_leg(idx)
                    self.undo_slot = (idx, leg)
                    self.sel = max(0, min(idx, len(self.state.legs) - 1))
                    self._ok(f"deleted leg {idx + 1}: {leg.label} - press u to undo")
                else:
                    self._ok("kept the leg")
            elif kind == "overwrite":
                target = self.pending_overwrite
                self.pending_overwrite = None
                if yes and target:
                    self._write_saved_file(target, overwrite=True)
                else:
                    self._ok("not saved")
            self.mode = "list"
            self.refresh_screen()

        # -- save ----------------------------------------------------------
        def _start_save(self) -> None:
            ok, err = self.state.can_build()
            if not ok:
                return self._err(err)
            self.text_kind = "save"
            self.mode = "text"
            self.refresh_screen()

        def _save_now(self, raw: str) -> None:
            target = raw or (str(self.save_path) if self.save_path else "itinerary.json")
            path = Path(target)
            if path.exists() and not self.overwrite:
                self.pending_overwrite = target
                self.yesno_kind = "overwrite"
                self.mode = "yesno"
                return self.refresh_screen()
            self._write_saved_file(target, overwrite=True)

        def _write_saved_file(self, target: str, overwrite: bool) -> None:
            try:
                saved = save_itinerary_file(self.state.build_trip(), Path(target),
                                            overwrite=overwrite)
            except (StopError, OSError) as exc:
                self._err(str(exc))
                self.mode = "list"
                return self.refresh_screen()
            self.saved_path = saved
            self._ok(f"saved {saved}")
            self.mode = "list"
            self.refresh_screen()

        # -- preferences ---------------------------------------------------
        def _text_submit(self) -> None:
            s = self.state
            raw = self._answer.value.strip()
            if self.text_kind == "currency":
                ok, err = s.set_currency(raw)
                if not ok:
                    return self._err(err)           # keeps what they typed
                self.pref_step = 1
            elif self.text_kind == "budget":
                ok, err = s.set_budget(raw)
                if not ok:
                    return self._err(err)
                self.pref_step = 2
            else:
                return self._save_now(raw)
            # No "currency = GBP" echo: the next question already shows the currency,
            # and Review shows currency, budget and GB/day in one line.
            self._status("")
            self._to_stage(1)

        def _move_choice(self, delta: int) -> None:
            self.data_sel = (self.data_sel + delta) % len(DATA_PRESETS)
            self.refresh_screen()

        def _confirm_choice(self) -> None:
            gb = DATA_PRESETS[self.data_sel][1]
            self.state.set_daily_data(gb)
            self._status("")     # Review restates the chosen GB/day in its total line
            self._to_stage(2)

        # -- finish / cancel ----------------------------------------------
        def _finish(self) -> None:
            s = self.state
            ok, err = s.can_build()
            if not ok:
                self.yesno_kind = ""
                self.mode = "list"
                self.refresh_screen()
                return self._err(err)
            trip = s.build_trip()
            if self.search is None:
                self.result = WizardResult(trip=trip, cancelled=False,
                                           save_path=self.saved_path)
                self.exit()
                return
            # A search callback was supplied: run it here so the terminal stays in the
            # interface instead of dropping to the shell with a one-line "Searching...".
            self._trip = trip
            self._start_search()

        # -- searching ----------------------------------------------------
        _FRAMES = "|/-\\"

        def _start_search(self) -> None:
            self.mode = "busy"
            self._elapsed = 0.0
            self._spinner = 0
            self._search_note = ""
            self.refresh_screen()
            self._ticker = self.set_interval(0.1, self._tick)
            self.run_worker(self._run_search, thread=True, exit_on_error=False)

        def _tick(self) -> None:
            self._elapsed += 0.1
            self._spinner += 1
            self._paint_busy()

        def _paint_busy(self) -> None:
            if self.mode == "busy":
                self._render_busy()

        def _render_busy(self) -> None:
            self._close_input()
            lines = self._list_lines(show_data=True, cursor=False) + [""]
            frame = self._FRAMES[self._spinner % len(self._FRAMES)]
            lines.append(f"  {frame}  Searching esimdb.com for this itinerary..."
                         f"   {self._elapsed:.0f} s")
            if self._search_note:
                lines.append(f"     {self._search_note}")
            self._st("body").update(Text("\n".join(lines)))
            self._status("")
            self._hint("querying esimdb.com  Ctrl+C interrupts")

        def _search_progress(self, step: int, total: int, label: str) -> None:
            """Called from the worker thread; the repaint is marshalled back."""
            self._search_note = f"{label} ({step} of {total})"
            self._safe_call(self._paint_busy)

        def _run_search(self) -> None:
            try:
                self._report = self.search(self._trip, self._search_progress)
            except Exception as exc:                       # reported, never a traceback
                self._search_error = str(exc) or type(exc).__name__
            self._safe_call(self._search_finished)

        def _safe_call(self, fn) -> None:
            try:
                self.call_from_thread(fn)
            except Exception:                              # the app is already closing
                pass

        def _search_finished(self) -> None:
            if self._ticker is not None:
                self._ticker.stop()
                self._ticker = None
            self.result = WizardResult(trip=self._trip, cancelled=False,
                                       save_path=self.saved_path, report=self._report,
                                       search_error=self._search_error or None)
            self.exit()

        def action_cancel(self) -> None:
            """Ctrl+C (and a double Esc) leaves with a clean cancellation."""
            self.result = WizardResult(trip=None, cancelled=True, save_path=self.saved_path)
            self.exit()

        # -- navigation ---------------------------------------------------
        def _cancel_entry(self) -> None:
            """Esc while a field is open: abandon that entry, keep everything else."""
            if self.mode == "form":
                self.mode = "list"
                self.form_edit_idx = None
                self._ok("cancelled - the leg is unchanged")
                return self.refresh_screen()
            if self.mode == "text" and self.text_kind == "save":
                self.mode = "list"
                self._ok("not saved")
                return self.refresh_screen()
            return self._back()

        def _back(self) -> None:
            if self.mode == "form":
                return self._cancel_entry()
            if self.mode == "yesno":
                return self._answer_yesno(False)
            if self.mode == "text" and self.text_kind == "save":
                self.mode = "list"
                self._ok("not saved")
                return self.refresh_screen()
            if self.stage == 2:
                self.pref_step = 2
                return self._to_stage(1)
            if self.stage == 1:
                self.pref_step = 0
                return self._to_stage(0)
            self._status("Itinerary is the first stage - press Esc twice to cancel")

        def _escape(self) -> None:
            if self.mode == "yesno":
                return self._answer_yesno(False)
            if self.mode == "form" or self.mode == "text":
                return self._cancel_entry()
            if self.stage == 0:
                if self._esc_armed:
                    return self.action_cancel()
                self._esc_armed = True
                return self._status("press Esc again to cancel the wizard "
                                    "(nothing is lost: use --itinerary to resume a saved one)")
            return self._back()

        # -- key routing --------------------------------------------------
        def on_input_submitted(self, event) -> None:
            event.stop()
            self._typing_submit()

        def _typing_submit(self) -> None:
            if self.mode == "form":
                self._form_submit()
            elif self.mode == "text":
                self._text_submit()

        def on_key(self, event) -> None:
            key = event.key
            if self.mode == "busy":
                # Swallow everything while searching so keys cannot queue up behind the
                # query; Ctrl+C stays live (priority binding) to interrupt it.
                if key != "ctrl+c":
                    event.stop()
                return
            if self._typing():
                # Letters, digits, arrows and Backspace belong to the text field:
                # navigation must never steal ordinary editing.
                if key == "escape":
                    event.stop()
                    return self._cancel_entry()
                if key == "alt+left":
                    event.stop()
                    return self._back()
                if self.focused is not self._answer:
                    # Focus can lag one key behind a fast burst ("a", then the country
                    # name in the same read), and the field receives nothing at all
                    # from that batch. Apply the edit here rather than losing it: an
                    # unanswered Enter in particular looks like a frozen wizard.
                    if key == "enter":
                        event.stop()
                        return self._typing_submit()
                    if key == "backspace":
                        event.stop()
                        return self._answer.action_delete_left()
                    character = event.character
                    if character and not key.startswith(("ctrl+", "alt+", "shift+")):
                        event.stop()
                        return self._answer.insert_text_at_cursor(character)
                return

            if key == "escape":
                event.stop()
                return self._escape()
            if key == "alt+left":
                event.stop()
                return self._back()

            if self.mode == "yesno":
                # Enter means "yes" everywhere except overwriting a file, where it
                # deliberately means "keep what is already there".
                enter_is_yes = self.yesno_kind != "overwrite"
                if key in ("y", "down", "right") or (key == "enter" and enter_is_yes):
                    event.stop()
                    return self._answer_yesno(True)
                if key in ("n", "up", "left") or (key == "enter" and not enter_is_yes):
                    event.stop()
                    return self._answer_yesno(False)
                return

            if self.mode == "choice":
                if key in ("up", "left"):
                    event.stop()
                    return self._move_choice(-1)
                if key in ("down", "right"):
                    event.stop()
                    return self._move_choice(1)
                if key == "enter":
                    event.stop()
                    return self._confirm_choice()
                return

            # Review is read-only, and Enter searches immediately: no cursor to move, no
            # leg commands, and no "are you sure?" page in front of the query.
            if self.stage == 2:
                if key == "enter":
                    event.stop()
                    return self._finish()
                if key == "s":
                    event.stop()
                    return self._start_save()
                if key in ("up", "down", "ctrl+p", "ctrl+n",
                           "a", "e", "d", "u", "t"):
                    event.stop()          # swallowed on purpose: nothing to move or edit
                return

            # list context (itinerary stage): these letters are commands
            if key in ("up", "ctrl+p"):
                event.stop()
                return self._move(-1)
            if key in ("down", "ctrl+n"):
                event.stop()
                return self._move(1)
            if key == "enter":
                event.stop()
                return self._continue()
            if key == "a":
                event.stop()
                return self._start_add()
            if key == "e":
                event.stop()
                return self._start_edit()
            if key == "d":
                event.stop()
                return self._ask_delete()
            if key == "u":
                event.stop()
                return self._undo_delete()
            if key == "t":
                event.stop()
                return self._start_transfer()
            if key == "s" and self.stage == 2:
                event.stop()
                return self._start_save()

        def _handle_exception(self, error: Exception) -> None:
            """Textual's crash hook: record a cancellation, then let Textual recover.

            Textual leaves the alternate screen and prints its own crash report around
            this call, so the terminal is never left grabbed. Recording a result here
            means the CLI reports a cancellation rather than doing nothing; and if a
            future Textual stops calling this hook, ``run_tui_wizard`` still treats a
            missing result as a cancellation.
            """
            if self.result is None:
                self.result = WizardResult(trip=None, cancelled=True,
                                           save_path=self.saved_path)
            self._crash_error = str(error)
            super()._handle_exception(error)




def run_tui_wizard(geo, state: WizardState, save_path: Path | None,
                   overwrite: bool, search: Callable | None = None) -> WizardResult:
    """Run the Textual wizard (blocks until the user finishes).

    ``mouse=False`` means the terminal is never asked to capture or report mouse
    events, so nothing in the wizard can be clicked and native terminal
    selection/copy keeps working.

    ``search`` (optional) is ``search(trip, progress) -> Report``. When given, the
    interview does not exit before the query: the search runs in a worker thread and
    the screen keeps updating with per-destination progress, so the user never sees the
    terminal return mid-operation.
    """
    app = WizardApp(geo=geo, state=state, save_path=save_path, overwrite=overwrite,
                    search=search)
    try:
        app.run(mouse=False)
    except (KeyboardInterrupt, EOFError):
        if app.result is None:
            app.result = WizardResult(trip=None, cancelled=True, save_path=None)
    if app.result is None:
        app.result = WizardResult(trip=None, cancelled=True, save_path=None)
    return app.result


# ---------------------------------------------------------------------------
# Dispatch (used by cmd_wizard) — picks the front-end by interactivity/flags
# ---------------------------------------------------------------------------
def wants_tui(stdin_tty: bool, stdout_tty: bool, stderr_tty: bool,
              json_mode: bool, plain_flag: bool) -> bool:
    """True when the in-place Textual wizard should be used."""
    if not stdin_tty or json_mode or plain_flag:
        return False
    return stdout_tty and stderr_tty


def run_wizard(
    geo,
    ask: Callable[[], str] | None = None,
    out: Callable[[str], None] | None = None,
    save_path: Path | None = None,
    overwrite: bool = False,
    default_currency: str = DEFAULT_CURRENCY,
    default_daily: float = DEFAULT_DAILY_DATA,
    plain: bool = True,
) -> WizardResult:
    """Convenience dispatcher. The CLI calls run_plain_wizard / run_tui_wizard
    directly so it can own the output streams; this exists for callers/tests."""
    state = WizardState()
    code, _ = validate_currency_code(default_currency)
    state.currency = code or DEFAULT_CURRENCY
    state.daily_data_gb = default_daily
    if plain:
        return run_plain_wizard(
            geo, state, ask or (lambda: input()), out or (lambda s="": print(s)),
            save_path, overwrite,
        )
    return run_tui_wizard(geo, state, save_path, overwrite)
