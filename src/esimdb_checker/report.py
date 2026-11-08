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
    return " · ".join(bits)


def _itinerary_lines(report: Report) -> list[str]:
    it = report.trip
    lines = [
        f"Itinerary: {it.total_days} travel day(s), {_gb(it.required_gb)} GB required, "
        f"currency {it.currency}"
        + (f", budget {_money(it.budget, it.currency)}" if it.budget is not None else ", no budget")
        + f", speed-capped: {_cap_state(report)}"
    ]
    lines.append(f"  {'Destination':<22} {'Days':>5}  {'Travel days':>12}  {'Required data':>14}")
    for leg in it.legs:
        lines.append(
            f"  {leg.country_name:<22} {leg.days:>5}  "
            f"{leg.travel_days_label():>12}  "
            f"{_gb(leg.required_gb) + ' GB':>14}"
        )
    lines.append(
        f"  {'TOTAL':<22} {it.total_days:>5}  {'1–' + str(it.total_days):>12}  "
        f"{_gb(it.required_gb) + ' GB':>14}"
    )
    return lines


def _cap_state(report: Report) -> str:
    s = report.speed_filter_status
    if s.get("include_capped"):
        return "INCLUDED (override)"
    if s.get("verified"):
        return "excluded (verified vs site toggle)"
    return "excluded (local predicate)"


def _strategy_table_lines(report: Report) -> list[str]:
    lines = ["Strategy comparison (cheapest feasible per family, ranked by requested currency):"]
    lines.append(
        f"  {'Family':<12} {'Total':>12} {'Purchases':>10} {'Data':>6} {'Validity':>10}  Feasible"
    )
    order = ["local_only", "whole_trip", "hybrid"]
    any_row = False
    for fam in order:
        s = report.strategies.get(fam)
        if s is None:
            lines.append(f"  {fam:<12} {'—':>12} {'—':>10} {'—':>6} {'—':>10}  infeasible")
            continue
        any_row = True
        cost = _money(s.total_cost, s.currency)
        valid = "ok" if s.validity_ok else "unverified"
        lines.append(
            f"  {fam:<12} {cost:>12} {s.purchase_count:>10} {'ok':>6} {valid:>10}  yes"
        )
    if not any_row:
        lines.append("  (no feasible strategy in any family)")
    return lines


def _legs_label(strategy: Strategy, report: Report) -> str:
    parts = []
    for sp in strategy.plans:
        for idx in sp.legs:
            leg = report.trip.legs[idx]
            parts.append(f"{leg.country_name} (days {leg.start_day}–{leg.end_day})")
    return ", ".join(parts)


def _strategy_detail_lines(report: Report, s: Strategy, title: str) -> list[str]:
    lines = [f"{title}"]
    for i, sp in enumerate(s.plans, start=1):
        activate_txt = _activation_text(sp, report)
        lines.append(f"  {i}. {sp.plan.name} — {sp.plan.provider}")
        lines.append(f"       activate: {activate_txt}")
        lines.append(f"       serves: {_legs_label(s, report) if len(s.plans) == 1 else _sp_legs(sp, report)}")
        price = _money(sp.cost, s.currency)
        lines.append(f"       price: {price}  ·  {_plan_bits(sp.plan)}")
    if s.assumptions:
        lines.append("  verification assumptions:")
        for a in s.assumptions:
            lines.append(f"    • {a}")
    return lines


def _sp_legs(sp: StrategyPlan, report: Report) -> str:
    parts = []
    for idx in sp.legs:
        leg = report.trip.legs[idx]
        parts.append(f"{leg.country_name} (days {leg.start_day}–{leg.end_day})")
    return ", ".join(parts)


def _activation_text(sp: StrategyPlan, report: Report) -> str:
    first = report.trip.legs[sp.legs[0]]
    if sp.activation_day == 1:
        where = "at the start of the trip"
    else:
        where = f"on arrival to {first.country_name} (travel day {sp.activation_day})"
    return where


def _recommendation_lines(report: Report) -> list[str]:
    rec = report.recommended
    if rec is None:
        lines = ["No feasible strategy was found."]
        reason = report.notes[0] if report.notes else "see diagnostics"
        lines.append(f"  {reason}")
        return lines
    bs = report.budget_status
    lines = [
        f"RECOMMENDED: {rec.family}  (cheapest price-comparable feasible strategy)"
    ]
    for i, sp in enumerate(rec.plans, start=1):
        lines.append(f"  buy {i}. {sp.plan.name} — {sp.provider if hasattr(sp, 'provider') else sp.plan.provider}")
        lines.append(f"      activate {_activation_text(sp, report)}")
        lines.append(f"      serves: {_sp_legs(sp, report)}")
        lines.append(f"      price: {_money(sp.cost, rec.currency)}  ·  {_plan_bits(sp.plan)}")
    # why it wins: savings vs other families
    others = {k: report.strategies.get(k) for k in ("local_only", "whole_trip", "hybrid") if k != rec.family}
    for fam, s in others.items():
        if s is not None and rec.total_cost is not None and s.total_cost is not None:
            diff = s.total_cost - rec.total_cost
            if diff > 0:
                lines.append(
                    f"  saves {_money(diff, rec.currency)} vs {fam} ({_money(s.total_cost, rec.currency)})"
                )
            elif diff < 0:
                lines.append(
                    f"  adds {_money(-diff, rec.currency)} vs {fam} ({_money(s.total_cost, rec.currency)})"
                )
    if rec.assumptions:
        lines.append("  verification assumptions:")
        for a in rec.assumptions:
            lines.append(f"    • {a}")
    # budget
    if bs.get("within_original"):
        lines.append(f"  budget: within {_money(bs['budget'], bs['currency'])} (cost {_money(bs['cost'], bs['currency'])})")
    else:
        lines.append(f"  budget: EXCEEDED — original {_money(bs['budget'], bs['currency'])}, cost {_money(bs['cost'], bs['currency'])}")
        if bs.get("relaxed"):
            lines.append(
                f"      relaxed to threshold {_money(bs['threshold'], bs['currency'])} after {bs['steps']} step(s); "
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
    excl = d.get("excluded_speed_cap", [])
    lines.append(f"  speed-capped excluded (local predicate): {len(excl)}")
    for e in excl[:8]:
        lines.append(f"    ✗ {e['name']} — {e['provider']} ({_money(e['price'], e['currency'])}): {'; '.join(e['reasons'])}")
    if len(excl) > 8:
        lines.append(f"    … {len(excl) - 8} more")
    near = d.get("near_matches", [])
    lines.append(f"  near-matches (fail validity, shown for reference — NOT valid): {len(near)}")
    for n in near[:8]:
        lines.append(f"    ? {n['leg']}: {n['plan']} — {n['provider']} ({_money(n['price'], n['currency'])}): {n['reason']}")
    unserv = d.get("unservable_legs", [])
    if unserv:
        lines.append(f"  unservable legs (no plan can meet data/validity): {', '.join(unserv)}")
    if d.get("fetch_failed_slugs"):
        lines.append(f"  acquisition failed for: {', '.join(d['fetch_failed_slugs'])}")
    return lines


def _speed_lines(report: Report) -> list[str]:
    s = report.speed_filter_status
    lines = ["Speed-cap requirement status:"]
    lines.append(f"  requirement: {s.get('requirement')}")
    lines.append(f"  mode: {s.get('mode')}" + ("  [VERIFIED]" if s.get("verified") else "  [NOT verified against the site toggle]"))
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


def render_console(report: Report) -> str:
    lines: list[str] = []
    lines.append("eSIM strategy recommendation")
    lines.append("=" * 78)
    lines.extend(_itinerary_lines(report))
    lines.append("")
    lines.extend(_strategy_table_lines(report))
    lines.append("")
    lines.extend(_recommendation_lines(report))
    lines.append("")
    lines.extend(_budget_lines(report))
    lines.append("")
    lines.extend(_diagnostics_lines(report))
    lines.append("")
    lines.extend(_speed_lines(report))
    lines.append("")
    lines.extend(_provenance_lines(report))
    return "\n".join(lines)


def render_markdown(report: Report) -> str:
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
        w(f"| {leg.country_name} | {leg.days} | {leg.travel_days_label()} | {_gb(leg.required_gb)} GB |")
    w(f"| **Total** | **{it.total_days}** | **1–{it.total_days}** | **{_gb(it.required_gb)} GB** |")
    w("")
    w("## Strategy comparison")
    w("")
    w("| Family | Total | Purchases | Data | Validity | Feasible |")
    w("|--------|-------|-----------|------|----------|----------|")
    for fam in ("local_only", "whole_trip", "hybrid"):
        s = report.strategies.get(fam)
        if s is None:
            w(f"| {fam} | — | — | — | — | infeasible |")
        else:
            w(f"| {fam} | {_money(s.total_cost, s.currency)} | {s.purchase_count} | ok | {'ok' if s.validity_ok else 'unverified'} | yes |")
    w("")
    w("## Recommendation")
    w("")
    rec = report.recommended
    if rec is None:
        w("No feasible strategy was found.")
        w("")
    else:
        w(f"**{rec.family}** — total {_money(rec.total_cost, rec.currency)}, {rec.purchase_count} purchase(s)")
        w("")
        for i, sp in enumerate(rec.plans, start=1):
            w(f"{i}. **{sp.plan.name}** — {sp.plan.provider}")
            w(f"   - activate: {_activation_text(sp, report)}")
            w(f"   - serves: {_sp_legs(sp, report)}")
            w(f"   - price: {_money(sp.cost, rec.currency)} · {_plan_bits(sp.plan)}")
        if rec.assumptions:
            w("")
            w("Verification assumptions:")
            for a in rec.assumptions:
                w(f"- {a}")
    w("")
    w("## Budget status")
    w("")
    bs = report.budget_status
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
    w("")
    w("## Diagnostics")
    w("")
    d = report.diagnostics
    w(f"Speed-capped excluded (local predicate): {len(d.get('excluded_speed_cap', []))}")
    for e in d.get("excluded_speed_cap", [])[:20]:
        w(f"- **{e['name']}** — {e['provider']} ({_money(e['price'], e['currency'])}): {'; '.join(e['reasons'])}")
    w(f"Near-matches (fail validity — NOT valid): {len(d.get('near_matches', []))}")
    for n in d.get("near_matches", [])[:20]:
        w(f"- {n['leg']}: **{n['plan']}** — {n['provider']} ({_money(n['price'], n['currency'])}): {n['reason']}")
    if d.get("unservable_legs"):
        w(f"Unserevable legs: {', '.join(d['unservable_legs'])}")
    w("")
    w("## Speed-cap requirement status")
    w("")
    s = report.speed_filter_status
    w(f"- requirement: {s.get('requirement')}")
    w(f"- mode: {s.get('mode')}" + (" (VERIFIED)" if s.get("verified") else " (NOT verified against the site toggle)"))
    w(f"- note: {s.get('note')}")
    w("")
    w("## Provenance & notes")
    w("")
    for n in report.notes:
        w(f"- {n}")
    w(f"- FX: {report.fx.get('base')} -> {report.fx.get('currency')} rate {report.fx.get('rate')} ({report.fx.get('source')}, date {report.fx.get('date')})")
    w("")
    w("### Snapshots")
    w("")
    for url, m in report.snapshot_manifest.items():
        w(f"- `{m.get('source', '?')}` {url} — sha256 `{(m.get('sha256') or '')[:12]}…`")
    w("")
    return "\n".join(md)


def write_report(report: Report, out_path: Path | None, fmt: str = "console") -> None:
    ext = {"markdown": "md", "json": "json", "console": "txt"}.get(fmt, "txt")
    if out_path is None:
        out_path = Path("report." + ext)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if fmt == "json":
        out_path.write_text(json.dumps(report.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")
    elif fmt == "markdown":
        out_path.write_text(render_markdown(report), encoding="utf-8")
    else:
        out_path.write_text(render_console(report), encoding="utf-8")


def render_json(report: Report) -> str:
    return json.dumps(report.to_dict(), indent=2, ensure_ascii=False)
