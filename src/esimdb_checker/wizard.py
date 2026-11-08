"""Interactive itinerary wizard.

One shared state machine (`WizardState`) is driven by two interchangeable
front-ends:

  * a compact in-place Textual TUI — an alternate-screen app that refreshes only
    its own area, restores the terminal on exit, and keeps validation errors in
    the current stage;
  * a plain line-oriented fallback (``--plain``, or when stdout is redirected /
    not a TTY).

Both front-ends use the SAME validation and the SAME ``build_trip`` /
``save_itinerary_file`` logic, so itinerary construction, currency validation,
and recommendation are never duplicated. Country resolution always uses the
shared ``Geo`` resolver.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, List, Optional

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
DATA_PRESETS: List[tuple[str, float]] = [
    ("Economy", 1.0),
    ("Balanced", 1.5),
    ("Higher usage", 2.0),
]

STAGES: List[str] = ["Destinations", "Preferences", "Review"]


@dataclass
class WizardResult:
    trip: Optional[Trip]
    cancelled: bool
    save_path: Optional[Path] = None


# ---------------------------------------------------------------------------
# Shared state machine
# ---------------------------------------------------------------------------
@dataclass
class LegEntry:
    country: Optional[Country]
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

    legs: List[LegEntry] = field(default_factory=list)
    currency: Optional[str] = None  # normalized supported code
    budget: Optional[float] = None  # None = no budget cap
    daily_data_gb: Optional[float] = None

    # ---- legs ----------------------------------------------------------
    def charged(self) -> List[LegEntry]:
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

    # ---- preferences (each returns (ok, error_or_none)) -----------------
    def set_currency(self, raw) -> tuple[bool, Optional[str]]:
        code, err = validate_currency_code(raw)
        if err is not None:
            return False, err
        self.currency = code
        return True, None

    def set_budget(self, raw) -> tuple[bool, Optional[str]]:
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

    def set_daily_data(self, raw) -> tuple[bool, Optional[str]]:
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

    def summary_lines(self, show_data: bool = True) -> List[str]:
        """Compact leg table with travel-day spans (mirrors the report's days).

        ``show_data`` is False during the destinations stage, before the daily
        data level has been chosen, so no GB figures are invented from a default.
        """
        daily = self.daily_data_for()
        lines: List[str] = []
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

    def review_lines(self) -> List[str]:
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


def _plain_review(state: WizardState, ask, out, save_path, overwrite) -> Optional[Path]:
    out("")
    out("Confirm your itinerary:")
    out("\n".join(state.review_lines()))
    if not _ask_yes_no(ask, out, "Search this itinerary?", default_yes=True):
        raise StopError("cancelled at the confirmation prompt (nothing searched)")
    return _plain_save(state, save_path, overwrite, ask, out)


def _plain_save(state, save_path, overwrite, ask, out) -> Optional[Path]:
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
    save_path: Optional[Path] = None,
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


def _ask_leg_index(state: WizardState, ask, out, prompt: str) -> Optional[int]:
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
# Textual TUI front-end (alternate screen, in-place refresh)
# ---------------------------------------------------------------------------
try:
    from textual.app import App
    from textual.containers import Horizontal
    from textual.widgets import Button, Input, Static

    _TEXTUAL = True
except Exception:  # pragma: no cover - Textual missing
    App = object  # type: ignore[misc,assignment]
    Horizontal = object  # type: ignore[misc,assignment]
    Button = Input = Static = object  # type: ignore[misc,assignment]
    _TEXTUAL = False


if _TEXTUAL:
    from rich.text import Text

    class WizardApp(App):
        """In-place itinerary wizard on the alternate screen."""

        CSS = """
        Screen { background: $background; }
        #header { text-style: bold; }
        #progress { padding: 0 1 0 1; }
        #body { padding: 0 1; height: auto; }
        #inputs { padding: 0 1; height: auto; }
        /* Horizontal children need an explicit width or each takes the full row. */
        #inputs Input { width: 1fr; margin-right: 1; }
        #inputs Input:last-of-type { margin-right: 0; }
        #buttons { padding: 0 1; }
        #status { padding: 0 1; min-height: 1; }
        #help { padding: 0 1; text-style: dim; }
        Button { margin-bottom: 0; }
        """

        def __init__(self, geo, state: WizardState, save_path: Optional[Path], overwrite: bool):
            super().__init__()
            self.geo = geo
            self.state = state
            self.save_path = save_path
            self.overwrite = overwrite
            self.stage = 0
            self.pref_step = 0          # Preferences: 0 currency, 1 budget, 2 daily
            self.mode = "add"           # Destinations: add|transfer|edit|remove
            self.edit_idx: Optional[int] = None
            self.data_sel = 1           # default selection: Balanced
            self.saved_path: Optional[Path] = None
            self.result: Optional[WizardResult] = None
            self._err_text = ""

        # -- compose ------------------------------------------------------
        def compose(self):
            yield Static("eSIM itinerary wizard", id="header")
            yield Static("", id="progress")
            yield Static("", id="body")
            yield Horizontal(
                Input(id="value", placeholder="destination"),
                Input(id="value2", placeholder="days", disabled=True),
                Input(id="value3", placeholder="transfer", disabled=True),
                id="inputs",
            )
            yield Horizontal(
                Button("add leg", variant="primary", id="btn_action"),
                Button("+ transfer", id="btn_transfer"),
                Button("edit", id="btn_edit"),
                Button("remove", id="btn_remove"),
                Button("save", id="btn_save"),
                Button("next ->", variant="primary", id="btn_next"),
                Button("< back", id="btn_back"),
                Button("cancel", variant="error", id="btn_cancel"),
            )
            yield Static("", id="status")
            yield Static("Tab: move focus  ·  Enter: confirm  ·  Esc: cancel", id="help")

        def on_mount(self):
            self.render_stage()

        # -- widget helpers ------------------------------------------------
        def _st(self, id: str) -> "Static":
            return self.query_one(f"#{id}", Static)

        def _in(self, id: str) -> "Input":
            return self.query_one(f"#{id}", Input)

        def _bt(self, id: str) -> "Button":
            return self.query_one(f"#{id}", Button)

        def _val(self, id: str) -> str:
            return self._in(id).value.strip()

        def _set(self, id: str, text: str) -> None:
            self._in(id).value = text

        def _progress_text(self) -> str:
            return "   ".join(
                ("● " if i <= self.stage else "○ ") + name for i, name in enumerate(STAGES)
            )

        def _status(self, text: str, err: bool = False) -> None:
            self._err_text = text if err else ""
            # A Text object (not a str) keeps button names like "[add leg]" literal:
            # Static would otherwise eat them as Rich markup tags.
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

        def _show_help(self, text: str) -> None:
            self._st("help").update(Text(text))

        # -- stage rendering ----------------------------------------------
        def render_stage(self) -> None:
            prog = self._st("progress")
            prog.update(self._progress_text())
            body = self._st("body")
            # reset controls to a neutral state first
            for id in ("value", "value2", "value3"):
                self._in(id).disabled = False
                self._in(id).placeholder = ""
            for id in ("btn_action", "btn_transfer", "btn_edit", "btn_remove",
                       "btn_save", "btn_next", "btn_back", "btn_cancel"):
                self._bt(id).disabled = False
                self._bt(id).display = True

            if self.stage == 0:
                self._render_destinations(body)
            elif self.stage == 1:
                self._render_preferences(body)
            else:
                self._render_review(body)

        def _render_destinations(self, body) -> None:
            lines = ["Add a leg to your itinerary:", ""]
            if self.state.legs:
                lines += self.state.summary_lines(show_data=False)
            else:
                lines.append("  (no legs yet)")
            body.update(Text("\n".join(lines)))
            self._in("value").placeholder = "destination name/code/slug"
            self._in("value2").placeholder = "days"
            self._in("value2").disabled = False
            self._in("value3").placeholder = "transfer days (0)"
            self._in("value3").disabled = False
            self._bt("btn_action").label = "add leg"
            self._bt("btn_transfer").display = True
            self._bt("btn_edit").display = True
            self._bt("btn_remove").display = True
            self._bt("btn_save").display = False
            self._bt("btn_next").label = "next ->"
            self._bt("btn_back").disabled = True
            if self.mode == "add":
                self._status("Enter a destination, its days, and transfer days, "
                             "then press [add leg] or Enter.")
            elif self.mode == "transfer":
                self._in("value").disabled = True
                self._in("value2").disabled = True
                self._status("Transfer leg: enter transfer days (0 = marker only) "
                             "in the last field, then Enter.")
            elif self.mode == "edit":
                self._status(f"Edit: type a leg number (1-{len(self.state.legs)}), "
                             "then press [edit] again or Enter.")
            elif self.mode == "remove":
                self._status(f"Remove: type a leg number (1-{len(self.state.legs)}), "
                             "then press [remove] again or Enter.")
            if self.mode == "edit" and self.edit_idx is not None:
                self._bt("btn_action").label = "save edit"
                self._bt("btn_edit").label = "cancel edit"
            else:
                self._bt("btn_action").label = "add leg"
                self._bt("btn_edit").label = "edit"
            if not self._in("value").disabled:
                self._in("value").focus()
            self._show_help("Tab: move focus · Enter: confirm · [edit]/[remove] then a leg # "
                            "· Esc: cancel")

        def _render_preferences(self, body) -> None:
            s = self.state
            if self.pref_step == 0:
                body.update(Text(
                    "Currency for prices and budget\n"
                    + "Supported: " + ", ".join(supported_currency_codes()) + "\n"
                    + f"Current: {s.currency or '(unset)'}"
                ))
                self._in("value").placeholder = "currency (e.g. EUR, USD, GBP)"
                self._set("value", s.currency or DEFAULT_CURRENCY)
                self._in("value2").disabled = True
                self._in("value3").disabled = True
                self._bt("btn_action").label = "use currency ->"
            elif self.pref_step == 1:
                budget_now = ("no budget cap" if s.budget is None
                              else f"{s.budget:g} {s.currency}")
                body.update(Text(
                    f"Total trip budget in {s.currency} (blank for no budget)\n"
                    + f"Current: {budget_now}"
                ))
                self._in("value").placeholder = "budget amount (blank = none)"
                self._set("value", "" if s.budget is None else f"{s.budget:g}")
                self._in("value2").disabled = True
                self._in("value3").disabled = True
                self._bt("btn_action").label = "use budget ->"
            else:
                total_days = sum(leg.days for leg in s.legs)
                lines = ["Expected data per destination day (arrow keys to select):"]
                for i, (name, gb) in enumerate(DATA_PRESETS):
                    marker = ">" if i == self.data_sel else " "
                    lines.append(f"  {marker} {name:<12} {gb:g} GB/day")
                cur = DATA_PRESETS[self.data_sel][1]
                lines.append(
                    f"\n  selected: {cur:g} GB/day  ->  {total_days} day trip "
                    f"uses {total_days * cur:g} GB"
                )
                body.update(Text("\n".join(lines)))
                self._in("value").disabled = True
                self._in("value2").disabled = True
                self._in("value3").disabled = True
                self._bt("btn_action").label = "confirm data"
                self._bt("btn_action").focus()
            self._bt("btn_transfer").display = False
            self._bt("btn_edit").display = False
            self._bt("btn_remove").display = False
            self._bt("btn_save").display = False
            self._bt("btn_next").display = False
            self._bt("btn_back").disabled = False
            if self.pref_step == 0:
                self._status("Enter a supported currency code, then press "
                             "[use currency ->] or Enter.")
            elif self.pref_step == 1:
                self._status("Enter a budget amount (blank for no budget), then Enter.")
            else:
                self._status("Use ↑/↓ to pick a data level, then Enter to confirm.")
            if self.pref_step != 2:
                self._in("value").focus()
            self._show_help("Tab: move focus · Enter: confirm · ↑/↓: pick data level · Esc: cancel")

        def _render_review(self, body) -> None:
            body.update(Text("Confirm your itinerary:\n"
                             + "\n".join(self.state.review_lines())))
            self._in("value").placeholder = "file path (save)"
            self._in("value").disabled = False
            self._in("value2").disabled = True
            self._in("value3").disabled = True
            self._bt("btn_action").label = "search this itinerary"
            self._bt("btn_save").display = True
            self._bt("btn_save").label = "save itinerary"
            self._bt("btn_transfer").display = False
            self._bt("btn_edit").display = False
            self._bt("btn_remove").display = False
            self._bt("btn_next").display = False
            self._bt("btn_back").disabled = False
            self._bt("btn_action").focus()
            self._status("Press [search this itinerary] to search, [save itinerary] to save, "
                         "or [< back] to edit.")
            self._show_help("Tab: move focus · Enter: search · [save itinerary] · Esc: cancel")

        def _render_search(self, body) -> None:  # pragma: no cover - search runs post-TUI
            body.update(Text("Searching esimdb.com ..."))
            for id in ("btn_action", "btn_transfer", "btn_edit", "btn_remove",
                       "btn_save", "btn_next", "btn_back", "btn_cancel"):
                self._bt(id).display = False
            self._in("value").disabled = True
            self._in("value2").disabled = True
            self._in("value3").disabled = True
            self._status("Working — the report is printed after this screen closes.")
            self._show_help("please wait")

        # -- actions ------------------------------------------------------
        def _leg_index_from(self, id: str) -> Optional[int]:
            v = self._val(id)
            if not v.isdigit():
                self._err(f"enter a leg number (1-{len(self.state.legs)})")
                return None
            n = int(v)
            if not (1 <= n <= len(self.state.legs)):
                self._err(f"leg number out of range (1-{len(self.state.legs)})")
                return None
            return n - 1

        def _clear_inputs(self) -> None:
            for id in ("value", "value2", "value3"):
                self._set(id, "")

        def _int_field(self, id: str, what: str, minimum: int):
            v = self._val(id)
            if v == "":
                self._err(f"enter a whole number for {what}")
                return None
            if not v.lstrip("-").isdigit():
                self._err(f"'{v}' is not a whole number")
                return None
            n = int(v)
            if n < minimum:
                self._err(f"{what} must be at least {minimum}")
                return None
            return n

        def _primary(self) -> None:
            if self.stage == 0:
                self._destinations_primary()
            elif self.stage == 1:
                self._preferences_primary()
            else:
                self._review_primary()

        def _destinations_primary(self) -> None:
            s = self.state
            if self.mode == "add":
                name = self._val("value")
                if not name:
                    return self._err("enter a destination (name, code, or slug)")
                c, err = _resolve(self.geo, name)
                if c is None:
                    return self._err(err)
                days = self._int_field("value2", "days", 1)
                if days is None:
                    return
                transfer = self._int_field("value3", "transfer days", 0)
                if transfer is None:
                    return
                s.add_destination(c, days, transfer)
                self._clear_inputs()
                extra = f" (+{transfer} transfer)" if transfer else ""
                self._ok(f"added {c.name}: {days} day(s){extra}")
                self.render_stage()
            elif self.mode == "transfer":
                transfer = self._int_field("value3", "transfer days", 0)
                if transfer is None:
                    return
                s.add_transfer(transfer)
                self._clear_inputs()
                self._ok(f"added transfer leg ({transfer} day(s), not charged)")
                self._set_mode("add")
            elif self.mode == "edit":
                if self.edit_idx is not None:
                    # Saving the leg that was loaded for editing.
                    days = self._int_field("value2", "days", 1)
                    if days is None:
                        return
                    transfer = self._int_field("value3", "transfer days", 0)
                    if transfer is None:
                        return
                    saved_idx = self.edit_idx
                    s.edit_leg(saved_idx, days, transfer)
                    self.edit_idx = None
                    self._clear_inputs()
                    self._ok(f"updated leg {saved_idx + 1}: {s.legs[saved_idx].label}")
                    self._set_mode("add")
                    return
                idx = self._leg_index_from("value")
                if idx is None:
                    return
                leg = s.legs[idx]
                if leg.country is None:
                    return self._err("transfer legs cannot be edited — remove it instead")
                self.edit_idx = idx
                self.render_stage()
                self._set("value", leg.country.name)
                self._set("value2", str(leg.days))
                self._set("value3", str(leg.transfer_days))
                self._in("value2").disabled = False
                self._in("value3").disabled = False
                self._status(f"Editing leg {idx + 1}: change days/transfer, then press "
                             "[save edit] or Enter ([edit] again cancels).")
                self._in("value2").focus()
            elif self.mode == "remove":
                idx = self._leg_index_from("value")
                if idx is None:
                    return
                out_label = s.legs[idx].label
                s.remove_leg(idx)
                self._clear_inputs()
                self._ok(f"removed: {out_label}")
                self._set_mode("add")

        def _set_mode(self, mode: str) -> None:
            self.mode = mode
            self.edit_idx = None
            self.render_stage()

        def _preferences_primary(self) -> None:
            s = self.state
            if self.pref_step == 0:
                ok, err = s.set_currency(self._val("value"))
                if not ok:
                    return self._err(err)
                self._ok(f"currency = {s.currency}")
                self.pref_step = 1
                self.render_stage()
            elif self.pref_step == 1:
                ok, err = s.set_budget(self._val("value"))
                if not ok:
                    return self._err(err)
                budget_txt = "set" if s.budget is None else f"= {s.budget:g} {s.currency}"
                self._ok(f"budget {budget_txt}")
                self.pref_step = 2
                self.render_stage()
            else:
                gb = DATA_PRESETS[self.data_sel][1]
                s.set_daily_data(gb)
                self._ok(f"using {gb:g} GB/day")
                self.stage = 2
                self.render_stage()

        def _review_primary(self) -> None:
            s = self.state
            ok, err = s.can_build()
            if not ok:
                return self._err(err)
            self.result = WizardResult(trip=s.build_trip(), cancelled=False,
                                       save_path=self.saved_path)
            self.exit()

        def _save_primary(self) -> None:
            raw = self._val("value").strip()
            # A bare yes (or an empty field) saves to the default path - never to a
            # file literally named "y", which is what a literal Path(raw) would do.
            affirmative = raw == "" or raw.lower() in ("y", "yes")
            target = ((str(self.save_path) if self.save_path else "itinerary.json")
                      if affirmative else raw)
            path = Path(target)
            overwrite = self.overwrite or (raw.lower() in ("y", "yes")
                                           and self.save_path is not None)
            try:
                saved = save_itinerary_file(self.state.build_trip(), path, overwrite=overwrite)
                self.saved_path = saved
                self._set("value", "")
                self._ok(f"saved itinerary to {saved}")
            except StopError as e:
                if "already exists" in str(e):
                    self._err(f"{path} already exists — type 'y' and press Enter to "
                              "overwrite, or [cancel] to skip.")
                else:
                    self._err(str(e))
            except OSError as e:
                self._err(f"could not save: {e}")

        def _next(self) -> None:
            if self.stage == 0:
                if not self.state.charged():
                    return self._err("add at least one destination leg (days > 0) first")
                self.stage = 1
                self.pref_step = 0
                self.render_stage()
            elif self.stage == 1:
                # advance within preferences
                self.pref_step = min(self.pref_step + 1, 2)
                self.render_stage()
            elif self.stage == 2:
                self._review_primary()

        def _back(self) -> None:
            if self.stage == 0:
                self._status("(first stage — press [cancel] to abort)")
            elif self.stage == 1:
                if self.pref_step > 0:
                    self.pref_step -= 1
                    self.render_stage()
                else:
                    self.stage = 0
                    self.render_stage()
            else:
                self.stage = 1
                self.pref_step = 2
                self.render_stage()

        def cancel(self) -> None:
            self.result = WizardResult(trip=None, cancelled=True, save_path=self.saved_path)
            self.exit()

        # -- event routing -------------------------------------------------
        def on_button_pressed(self, event: "Button.Pressed") -> None:
            bid = event.button.id
            if bid == "btn_action":
                self._primary()
            elif bid == "btn_transfer":
                self._set_mode("transfer")
            elif bid == "btn_edit":
                if self.mode == "edit" and self.edit_idx is not None:
                    self._set_mode("add")            # abandon an in-progress edit
                elif self.mode == "edit":
                    self._destinations_primary()     # pick the leg number that was typed
                else:
                    self._set_mode("edit")
            elif bid == "btn_remove":
                if self.mode == "remove":
                    self._destinations_primary()     # remove the leg number that was typed
                else:
                    self._set_mode("remove")
            elif bid == "btn_save":
                self._save_primary()
            elif bid == "btn_next":
                self._next()
            elif bid == "btn_back":
                self._back()
            elif bid == "btn_cancel":
                self.cancel()

        def on_key(self, event) -> None:
            if event.key == "enter":
                focused = self.focused
                if isinstance(focused, Button):
                    focused.press()
                else:
                    self._primary()
                event.stop()
            elif event.key == "escape":
                self.cancel()
                event.stop()
            elif self.stage == 1 and self.pref_step == 2 and event.key in (
                "up", "down", "left", "right",
            ):
                n = len(DATA_PRESETS)
                if event.key in ("up", "left"):
                    self.data_sel = (self.data_sel - 1) % n
                else:
                    self.data_sel = (self.data_sel + 1) % n
                self._render_preferences(self._st("body"))
                event.stop()

        def on_exception(self, exception: Exception) -> None:
            # Never leave the terminal unusable; show a concise error and exit.
            self._status(f"unexpected error: {exception}", err=True)
            if self.result is None:
                self.result = WizardResult(trip=None, cancelled=True, save_path=self.saved_path)
            self.exit()


def run_tui_wizard(geo, state: WizardState, save_path: Optional[Path],
                   overwrite: bool) -> WizardResult:
    """Run the Textual wizard (blocks until the user finishes)."""
    app = WizardApp(geo=geo, state=state, save_path=save_path, overwrite=overwrite)
    try:
        app.run()
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
    ask: Optional[Callable[[], str]] = None,
    out: Optional[Callable[[str], None]] = None,
    save_path: Optional[Path] = None,
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
