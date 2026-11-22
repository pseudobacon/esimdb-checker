"""Render a Report to the console, markdown, or JSON.

Console/Markdown lead with the itinerary table and the complete strategy
comparison (local-only vs whole-trip vs hybrid), then the recommended strategy
(what to buy, when to activate, which legs, why it wins, budget status), then
diagnostics and provenance. JSON carries the full typed structure, including all
itinerary legs and derived fields.
"""

from __future__ import annotations

import json
from pathlib import Path

from . import filters
from .models import Report, Strategy, StrategyPlan


def _money(v, currency: str) -> str:
    if v is None:
        return "n/a"
    sym = {"EUR": "€", "USD": "$", "GBP": "£"}.get(currency, currency + " ")
    if float(v).is_integer():
        return f"{sym}{int(v):,}"
    return f"{sym}{v:,.2f}"


def _gb(v) -> str:
    if v is None:
        return "n/a"
    if float(v).is_integer():
        return f"{int(v)}"
    return f"{v:g}"


def _plan_bits(plan) -> str:
    bits = [f"{plan.capacity_label()} data"]
    if plan.period_label() != "—":
        bits.append(plan.period_label())
    cap = plan.data_cap_label()
    if cap:
        bits.append(cap)
    # A plan shown despite a speed cap must say so on its own line: an unlimited-at-1Mbps
    # plan and a clean 30 GB plan would otherwise be indistinguishable in the buy list.
    warning = filters.speed_cap_label(plan)
    if warning:
        bits.append(f"⚠ {warning}")
    return " · ".join(bits)


def _itinerary_lines(report: Report) -> list[str]:
    it = report.trip
    lines = [
        f"Itinerary: {it.total_days} charged day(s) over {it.span_days} travel day(s), "
        f"{_gb(it.required_gb)} GB required, "
        f"currency {it.currency}"
        + (f", budget {_money(it.budget, it.currency)}" if it.budget is not None else ", no budget")
        + f", speed-capped: {_cap_state(report)}"
        + _activation_state(report)
        + _transfer_state(report)
    ]
    lines.append(f"  {'Destination':<22} {'Days':>5}  {'Travel days':>12}  {'Required data':>14}")
    for leg in it.legs:
        lines.append(
            f"  {leg.country_name:<22} {leg.days:>5}  "
            f"{leg.travel_days_label():>12}  "
            f"{_gb(leg.required_gb) + ' GB':>14}"
        )
    lines.append(
        f"  {'TOTAL':<22} {it.total_days:>5}  {'1–' + str(it.span_days):>12}  "
        f"{_gb(it.required_gb) + ' GB':>14}"
    )
    return lines


def _cap_state(report: Report) -> str:
    s = report.speed_filter_status
    if s.get("include_capped"):
        return "capped INCLUDED (override)"
    if s.get("strict_speed_cap"):
        return "all speed caps excluded (--strict-speed-cap)"
    if s.get("verified"):
        return "always-throttled excluded (verified vs site toggle)"
    return "always-throttled excluded, after-allowance caps allowed"


def _frontier_lines(report: Report) -> list[str]:
    """The purchase-count trade-off: what each number of eSIMs costs.

    Rendered as one boxed table - the per-family strategy table used to sit beside it,
    but every rung already says what it buys, so the family table was redundant.
    """
    rows = report.purchase_frontier
    if not rows:
        return []
    lines: list[str] = []
    chosen = next((r for r in rows if r.get("recommended")), None)
    fewest = rows[0]
    if chosen is not None and fewest["purchases"] < chosen["purchases"]:
        n = fewest["purchases"]
        pct = fewest.get("vs_cheapest_pct") or 0.0
        plans = "; ".join(
            f"{p['name']} — {p['provider']} ({_money(p['price'], p['currency'])}, "
            f"{'+'.join(p['legs'])})"
            for p in fewest["plans"]
        )
        lines.append(
            f"Fewest that work: {n} eSIM{'s' if n != 1 else ''} for "
            f"{_money(fewest['total_cost'], fewest['currency'])} (+{pct:.1f}% vs cheapest)"
        )
        lines.append(f"  {plans}")
    lines.extend(_frontier_table(report))
    lines.extend(_expanded_rows(report))
    return lines


def _frontier_table(report: Report) -> list[str]:
    """The boxed ladder: eSIMs | Total | vs cheapest | what it buys."""
    rows = report.purchase_frontier
    if not rows:
        return []
    head = ("eSIMs", "Total", "vs cheapest", "What it buys")
    data = []
    for r in rows:
        pct = r.get("vs_cheapest_pct")
        vs = "cheapest" if pct is None or abs(pct) < 0.05 else f"+{pct:.1f}%"
        s = report.ladder_strategies.get(r["purchases"])
        buys = _buys_summary(s, report) if s is not None else "—"
        if r.get("buys_same_plan_twice"):
            buys += " (one plan bought twice)"
        if r.get("recommended"):
            buys = "▸ " + buys
        data.append((str(r["purchases"]), _money(r["total_cost"], r["currency"]), vs, buys))
    widths = [max(len(head[i]), max(len(row[i]) for row in data)) for i in range(4)]

    def hline(left: str, mid: str, right: str) -> str:
        return left + mid.join("─" * (w + 2) for w in widths) + right

    def cells(t: tuple[str, ...]) -> str:
        parts = []
        for i, (c, w) in enumerate(zip(t, widths, strict=False)):
            cell = f" {c} "
            parts.append(cell.rjust(w + 2) if i < 3 else cell.ljust(w + 2))
        return "│" + "│".join(parts) + "│"

    lines = [hline("┌", "┬", "┐"), cells(head), hline("├", "┼", "┤")]
    lines.extend(cells(r) for r in data)
    lines.append(hline("└", "┴", "┘"))
    lines.append("  (▸ = recommended)")
    return lines


def _buys_summary(s: Strategy, report: Report) -> str:
    """A short 'what this rung buys' label for the ladder table."""
    n = len(s.plans)
    if s.family == "local_only":
        stays = sum(1 for leg in report.trip.legs if leg.days > 0)
        return f"locals for all {n} stays" if n == stays else f"{n} local plans"
    if n == 1:
        p = s.plans[0].plan
        if p.unlimited:
            cap = "unlimited"
        elif p.is_daily_refill():
            cap = f"{p.capacity_mb / 1000:g} GB/day"
        else:
            g = p.capacity_mb / 1000  # base-1000, the way the site labels plans
            cap = f"{g:.0f} GB" if abs(g - round(g)) < 0.05 else f"{g:g} GB"
        period = f"/{p.period_days}d" if p.period_days else ""
        return f"{p.provider} {cap}{period}"
    if n <= 3:
        short = [name if len(name) <= 24 else name[:23] + "…"
                 for name in (sp.plan.name for sp in s.plans)]
        return " + ".join(short)
    return f"{n} plans (regional + locals)"


def _expanded_rows(report: Report) -> list[str]:
    """Buy lists for the ladder rows named by --show-purchases.

    The ladder prices every number of eSIMs, but only the recommendation says what you would
    install - so comparing rows meant re-running the tool once per row. These blocks price a
    row and then spell it out, in the same words the recommendation uses: the same install
    list, and the same verification assumptions (activation timing, validity span, any speed
    cap that was accepted), so an alternative is never described less honestly than the answer.
    """
    wanted = (report.totals or {}).get("show_purchases") or []
    if not wanted:
        return []
    rows = {r["purchases"]: r for r in report.purchase_frontier}
    lines: list[str] = []
    for k in wanted:
        row, s = rows.get(k), report.ladder_strategies.get(k)
        lines.append("")
        if row is None or s is None:
            lines.append(f"  Nothing feasible buys exactly {k} eSIM{'s' if k != 1 else ''}; "
                         "cannot show a buy list.")
            continue
        pct = row.get("vs_cheapest_pct")
        vs = ("the cheapest" if pct is None or abs(pct) < 0.05 else f"+{pct:.1f}% vs cheapest")
        tag = " - and this is the recommendation" if row.get("recommended") else ""
        lines.append(
            f"  What {k} eSIM{'s' if k != 1 else ''} would buy "
            f"({_money(row['total_cost'], row['currency'])}, {vs}){tag}:"
        )
        lines.extend(_buy_lines(report, s))
    return lines


def _legs_label(strategy: Strategy, report: Report) -> str:
    parts = []
    for sp in strategy.plans:
        for idx in sp.legs:
            leg = report.trip.legs[idx]
            parts.append(f"{leg.country_name} (days {leg.start_day}–{leg.end_day})")
    return ", ".join(parts)


def _buy_lines(report: Report, s: Strategy) -> list[str]:
    """The 'buy N. …' install list for one strategy.

    Shared by the recommendation and by an expanded ladder row on purpose: an alternative you
    are asked to consider has to be described in exactly the same detail as the answer, or the
    comparison is rigged by formatting.
    """
    lines = []
    for i, sp in enumerate(s.plans, start=1):
        provider = sp.provider if hasattr(sp, "provider") else sp.plan.provider
        lines.append(f"  buy {i}. {sp.plan.name} — {provider}")
        lines.append(f"      activate {_activation_text(sp, report)}")
        lines.append(f"      serves: {_sp_legs(sp, report)}")
        lines.append(f"      price: {_money(sp.cost, s.currency)}  ·  {_plan_bits(sp.plan)}")
    return lines


def _sp_legs(sp: StrategyPlan, report: Report) -> str:
    parts = []
    for idx in sp.legs:
        leg = report.trip.legs[idx]
        parts.append(f"{leg.country_name} (days {leg.start_day}–{leg.end_day})")
    return ", ".join(parts)


def _activation_text(sp: StrategyPlan, report: Report) -> str:
    first = report.trip.legs[sp.legs[0]]
    day, lead = sp.activation_day, first.start_day - sp.activation_day
    if day < 1:
        when = f"the day before the trip starts (travel day {day})"
    elif day == 1 and lead <= 0:
        when = "at the start of the trip"
    else:
        when = f"travel day {day}"
    gap = (f", {lead} day{'s' if lead != 1 else ''} before arrival to {first.country_name}"
           if lead > 0 else "")
    return f"{when}{gap}"


def _activation_state(report: Report) -> str:
    lead = (report.totals or {}).get("activation_lead_days") or 0
    if not lead:
        return ""
    return (f", activation: {lead} day{'s' if lead != 1 else ''} before the first arrival "
            f"each plan serves")


def _transfer_state(report: Report) -> str:
    f = report.trip.transfer_data
    if not f:
        return ""
    return f", flying days: {f:g}x the daily data need"


def _recommendation_lines(report: Report) -> list[str]:
    rec = report.recommended
    if rec is None:
        lines = ["No feasible strategy was found."]
        reason = report.notes[0] if report.notes else "see diagnostics"
        lines.append(f"  {reason}")
        return lines
    bs = report.budget_status
    policy = report.purchase_policy or {}
    n = rec.purchase_count
    esims = "eSIM" if n == 1 else "eSIMs"
    rule = policy.get("rule", "cheapest")
    limit = policy.get("max_purchases")
    if rule == "fewer_purchases":
        pct = policy.get("fewer_sims_pct")
        why = f"fewest {esims} within {pct:g}% of the cheapest total"
    elif rule == "fewest_within_limit":
        pct = policy.get("fewer_sims_pct")
        why = (f"fewest eSIMs the {limit}-eSIM limit allows (none of them are within "
               f"{pct:g}% of the cheapest total)")
    elif limit is not None:
        why = f"cheapest price-comparable strategy within {limit} eSIM{'s' if limit != 1 else ''}"
    else:
        why = "cheapest price-comparable feasible strategy"
    lines = [f"RECOMMENDED: {rec.family}  ({why})"]
    lines.extend(_buy_lines(report, rec))
    # why it wins: savings vs other families
    others = {
        k: report.strategies.get(k)
        for k in ("local_only", "whole_trip", "hybrid") if k != rec.family
    }
    for fam, s in others.items():
        if s is not None and rec.total_cost is not None and s.total_cost is not None:
            diff = s.total_cost - rec.total_cost
            if diff > 0:
                lines.append(
                    f"  saves {_money(diff, rec.currency)} vs {fam} "
                    f"({_money(s.total_cost, rec.currency)})"
                )
            elif diff < 0:
                lines.append(
                    f"  adds {_money(-diff, rec.currency)} vs {fam} "
                    f"({_money(s.total_cost, rec.currency)})"
                )
    # budget (only when one was actually set - "within n/a" says nothing)
    if bs.get("budget") is not None and bs.get("within_original"):
        lines.append(
            f"  budget: within {_money(bs['budget'], bs['currency'])} "
            f"(cost {_money(bs['cost'], bs['currency'])})"
        )
    elif bs.get("budget") is not None:
        lines.append(
            f"  budget: EXCEEDED — original {_money(bs['budget'], bs['currency'])}, "
            f"cost {_money(bs['cost'], bs['currency'])}"
        )
        if bs.get("relaxed"):
            lines.append(
                f"      relaxed to threshold {_money(bs['threshold'], bs['currency'])} "
                f"after {bs['steps']} step(s); "
                f"overspend {_money(bs['overspend'], bs['currency'])}"
            )
    return lines


def _budget_lines(report: Report) -> list[str]:
    bs = report.budget_status
    if bs.get("budget") is None:
        return ["Budget: none specified."]
    if bs.get("within_original"):
        return [f"Budget: within original budget of {_money(bs['budget'], bs['currency'])}."]
    out = ["Budget status:"]
    out.append(f"  • {bs['message']}")
    out.append(f"  • actual cost: {_money(bs['cost'], bs['currency'])}")
    if bs.get("relaxed"):
        out.append(f"  • successful threshold: {_money(bs['threshold'], bs['currency'])}")
        out.append(f"  • overspend over original: {_money(bs['overspend'], bs['currency'])}")
        out.append(f"  • relaxation steps: {bs['steps']}")
    return out


def _diagnostics_lines(report: Report) -> list[str]:
    d = report.diagnostics
    lines = ["Diagnostics (not part of the recommendation):"]
    # Plans below the trip's data need are filtered before ranking and never were
    # candidates, so they are counted rather than listed.
    data_filtered = d.get("data_insufficient_filtered", 0)
    if data_filtered:
        lines.append(f"  filtered before ranking (data below this trip's need): "
                     f"{data_filtered}")
    throttled = d.get("excluded_always_throttled", [])
    # The *_total key is the real count; the detail list is capped and must never be read as one.
    total = d.get("excluded_always_throttled_total", len(throttled))
    if total:
        lines.append(f"  throttled from first use, excluded: {total}")
    for e in throttled[:8]:
        lines.append(f"    ✗ {e['name']} — {e['provider']} "
                     f"({_money(e['price'], e['currency'])}): {'; '.join(e['reasons'])}")
    if total > len(throttled):
        lines.append(f"    … {total - len(throttled)} more ({len(throttled)} shown)")
    elif len(throttled) > 8:
        lines.append(f"    … {len(throttled) - 8} more")
    # After-allowance throttles are candidates now, so they are not exclusions - but a
    # run where they did the work should not look identical to one where none existed.
    admitted = d.get("after_cap_admitted", 0)
    if admitted:
        lines.append(f"  throttled only after the allowance, kept as candidates: {admitted}")
    strict_excluded = d.get("after_cap_excluded_strict", 0)
    if strict_excluded:
        lines.append(f"  throttled only after the allowance, excluded by --strict-speed-cap: "
                     f"{strict_excluded}")
    near = d.get("near_matches", [])
    lines.append(f"  near-matches (fail validity, shown for reference — NOT valid): {len(near)}")
    for n in near[:8]:
        lines.append(
            f"    ? {n['leg']}: {n['plan']} — {n['provider']} "
            f"({_money(n['price'], n['currency'])}): {n['reason']}"
        )
    reg_near = d.get("regional_near_matches", [])
    reg_near_total = d.get("regional_near_matches_total", len(reg_near))
    if reg_near_total:
        lines.append(
            f"  multi-country plans with enough data but not enough calendar: "
            f"{reg_near_total}"
        )
        for e in reg_near[:6]:
            lines.append(
                f"    ? {e['name']} — {e['provider']} "
                f"({_money(e['price'], e['currency']) if e['price'] is not None else 'no price'}): "
                f"{e['reason']}"
            )
        if reg_near_total > len(reg_near):
            lines.append(f"    … {reg_near_total - len(reg_near)} more ({len(reg_near)} shown)")
    alts = d.get("regional_alternatives", [])
    alt_total = d.get("regional_alternatives_total", len(alts))
    if alt_total:
        lines.append(
            f"  other multi-leg plans that work, at a higher cost than the "
            f"recommendation: {alt_total}"
        )
        for a in alts[:6]:
            lines.append(
                f"    ≈ {a['name']} — {a['provider']} "
                f"({_money(a['price'], a['currency']) if a['price'] is not None else 'no price'}): "
                f"covers {' + '.join(a['legs'])} (travel days {a['covers_days']})"
            )
        if alt_total > len(alts):
            lines.append(f"    … {alt_total - len(alts)} more ({len(alts)} shown)")
    unserv = d.get("unservable_legs", [])
    if unserv:
        lines.append(f"  unservable legs (no plan can meet data/validity): {', '.join(unserv)}")
    if d.get("unpurchasable_excluded"):
        lines.append(f"  not purchasable (no positive price): {d['unpurchasable_excluded']}")
    if d.get("provider_filtered_out"):
        lines.append(f"  filtered out by --provider: {d['provider_filtered_out']}")
    if d.get("fetch_failed_slugs"):
        lines.append(f"  acquisition failed for: {', '.join(d['fetch_failed_slugs'])}")
    return lines


def _speed_lines(report: Report) -> list[str]:
    s = report.speed_filter_status
    lines = ["Speed-cap requirement status:"]
    lines.append(f"  requirement: {s.get('requirement')}")
    lines.append(
        f"  mode: {s.get('mode')}"
        + ("  [VERIFIED]" if s.get("verified") else "  [NOT verified against the site toggle]")
    )
    lines.append(f"  note: {s.get('note')}")
    return lines


def _provenance_lines(report: Report) -> list[str]:
    lines = ["Provenance / notes:"]
    for n in report.notes:
        lines.append(f"   • {n}")
    lines.append(f"   generated {report.generated_at}")
    lines.append("")
    lines.append("Snapshots (raw responses cached for reproducibility):")
    for url, m in report.snapshot_manifest.items():
        short = url.replace("https://esimdb.com", "")
        lines.append(f"   {m.get('source', '?')}: {short}  sha256={(m.get('sha256') or '')[:12]}…")
    fx = report.fx
    lines.append(
        f"   FX: base {fx.get('base')} -> {fx.get('currency')} rate {fx.get('rate')} "
        f"({fx.get('source')}, date {fx.get('date')})"
    )
    return lines


def render_console(report: Report, verbose: bool = False) -> str:
    """The default report: the answer, not the audit trail.

    Itinerary, the ladder table, the expanded rows (if --show-purchases), the
    recommendation, and the budget line when a budget was given. With ``verbose``,
    the audit trail comes back: diagnostics, speed-cap status, provenance/notes,
    snapshots, and the no-budget note.
    """
    lines: list[str] = []
    lines.append("eSIM strategy recommendation")
    lines.append("=" * 78)
    lines.extend(_itinerary_lines(report))
    lines.append("")
    lines.extend(_frontier_lines(report))
    lines.append("")
    lines.extend(_recommendation_lines(report))
    lines.append("")
    if report.budget_status.get("budget") is not None or verbose:
        lines.extend(_budget_lines(report))
        lines.append("")
    if verbose:
        lines.extend(_diagnostics_lines(report))
        lines.append("")
        lines.extend(_speed_lines(report))
        lines.append("")
        lines.extend(_provenance_lines(report))
    return "\n".join(lines)


def render_markdown(report: Report, verbose: bool = False) -> str:
    it = report.trip
    md: list[str] = []
    w = md.append
    w("# eSIM Strategy Recommendation")
    w("")
    w(f"- **Generated:** {report.generated_at}")
    w(f"- **Currency:** {it.currency}")
    if it.budget is not None:
        w(f"- **Budget:** {_money(it.budget, it.currency)}")
    w("")
    w("## Itinerary")
    w("")
    w("| Destination | Days | Travel days | Required data |")
    w("|-------------|------|-------------|---------------|")
    for leg in it.legs:
        w(f"| {leg.country_name} | {leg.days} | {leg.travel_days_label()} "
          f"| {_gb(leg.required_gb)} GB |")
    w(f"| **Total** | **{it.total_days}** | **1–{it.span_days}** | **{_gb(it.required_gb)} GB** |")
    w("")
    if report.purchase_frontier:
        w("## Cost of convenience")
        w("")
        w("| eSIMs | Total | vs cheapest | What it buys |")
        w("|-------|-------|-------------|-------------|")
        for r in report.purchase_frontier:
            pct = r.get("vs_cheapest_pct")
            vs = "cheapest" if pct is None or abs(pct) < 0.05 else f"+{pct:.1f}%"
            s = report.ladder_strategies.get(r["purchases"])
            buys = _buys_summary(s, report) if s is not None else "—"
            if r.get("buys_same_plan_twice"):
                buys += " (one plan bought twice)"
            if r.get("recommended"):
                buys = "▸ " + buys
            w(f"| {r['purchases']} | {_money(r['total_cost'], r['currency'])} | {vs} | {buys} |")
        w("")
        w("(▸ = recommended)")
        chosen = next((r for r in report.purchase_frontier if r.get("recommended")), None)
        fewest = report.purchase_frontier[0]
        if chosen is not None and fewest["purchases"] < chosen["purchases"]:
            n = fewest["purchases"]
            w("")
            w(f"Fewest that work: **{n} eSIM{'s' if n != 1 else ''}** for "
              f"**{_money(fewest['total_cost'], fewest['currency'])}** "
              f"(+{fewest.get('vs_cheapest_pct') or 0.0:.1f}% vs cheapest) — "
              + "; ".join(
                  f"{p['name']} ({_money(p['price'], p['currency'])}: {'+'.join(p['legs'])})"
                  for p in fewest["plans"]
              ))
        w("")
        for k in (report.totals or {}).get("show_purchases") or []:
            row = next((r for r in report.purchase_frontier if r["purchases"] == k), None)
            s = report.ladder_strategies.get(k)
            if row is None or s is None:
                w(f"Nothing feasible buys exactly {k} eSIM{'s' if k != 1 else ''}.")
                w("")
                continue
            pct = row.get("vs_cheapest_pct")
            vs = "the cheapest" if pct is None or abs(pct) < 0.05 else f"+{pct:.1f}% vs cheapest"
            w(f"**What {k} eSIM{'s' if k != 1 else ''} would buy** "
              f"(**{_money(row['total_cost'], row['currency'])}**, {vs}) — "
              f"{'_recommended_' if row.get('recommended') else ''}")
            for sp in s.plans:
                w(f"- **{sp.plan.name}** — {sp.plan.provider}: {_money(sp.cost, s.currency)}, "
                  f"activate {_activation_text(sp, report)}, serves {_sp_legs(sp, report)}, "
                  f"{_plan_bits(sp.plan)}")
            w("")
    w("## Recommendation")
    w("")
    rec = report.recommended
    if rec is None:
        w("No feasible strategy was found.")
        w("")
    else:
        w(
            f"**{rec.family}** — total {_money(rec.total_cost, rec.currency)}, "
            f"{rec.purchase_count} purchase(s)"
        )
        w("")
        for i, sp in enumerate(rec.plans, start=1):
            w(f"{i}. **{sp.plan.name}** — {sp.plan.provider}")
            w(f"   - activate: {_activation_text(sp, report)}")
            w(f"   - serves: {_sp_legs(sp, report)}")
            w(f"   - price: {_money(sp.cost, rec.currency)} · {_plan_bits(sp.plan)}")
    bs = report.budget_status
    if bs.get("budget") is not None or verbose:
        w("")
        w("## Budget status")
        w("")
        if bs.get("budget") is None:
            w("No budget specified.")
        elif bs.get("within_original"):
            w(f"Within the original budget of {_money(bs['budget'], bs['currency'])}.")
        else:
            w(bs["message"])
            w(f"- actual cost: {_money(bs['cost'], bs['currency'])}")
            if bs.get("relaxed"):
                w(f"- successful threshold: {_money(bs['threshold'], bs['currency'])}")
                w(f"- overspend over original: {_money(bs['overspend'], bs['currency'])}")
                w(f"- relaxation steps: {bs['steps']}")
    if verbose:
        w("")
        w("## Diagnostics")
        w("")
        # Same lines as the console report - a second copy of this logic is how the two
        # renderers ended up disagreeing about whether counts were truncated.
        for line in _diagnostics_lines(report)[1:]:
            stripped = line.strip()
            if not stripped:
                continue
            w(f"- {stripped.lstrip('✗? ')}" if line.startswith("    ") else stripped)
        w("")
        w("## Speed-cap requirement status")
        w("")
        s = report.speed_filter_status
        w(f"- requirement: {s.get('requirement')}")
        w(f"- mode: {s.get('mode')}"
          + (" (VERIFIED)" if s.get("verified") else " (NOT verified against the site toggle)"))
        w(f"- note: {s.get('note')}")
        w("")
        w("## Provenance & notes")
        w("")
        for n in report.notes:
            w(f"- {n}")
        w(
            f"- FX: {report.fx.get('base')} -> {report.fx.get('currency')} "
            f"rate {report.fx.get('rate')} "
            f"({report.fx.get('source')}, date {report.fx.get('date')})"
        )
        w("")
        w("### Snapshots")
        w("")
        for url, m in report.snapshot_manifest.items():
            w(f"- `{m.get('source', '?')}` {url} — sha256 `{(m.get('sha256') or '')[:12]}…`")
        w("")
    return "\n".join(md)


def write_report(report: Report, out_path: Path | None, fmt: str = "console",
                 verbose: bool = False) -> None:
    ext = {"markdown": "md", "json": "json", "console": "txt"}.get(fmt, "txt")
    if out_path is None:
        out_path = Path("report." + ext)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if fmt == "json":
        # JSON is the machine-readable record: it stays complete regardless of --verbose.
        out_path.write_text(
            json.dumps(report.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8"
        )
    elif fmt == "markdown":
        out_path.write_text(render_markdown(report, verbose=verbose), encoding="utf-8")
    else:
        out_path.write_text(render_console(report, verbose=verbose), encoding="utf-8")


def render_json(report: Report) -> str:
    return json.dumps(report.to_dict(), indent=2, ensure_ascii=False)
