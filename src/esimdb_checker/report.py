"""Render a Report to the console, markdown, or JSON."""

from __future__ import annotations

import json
from pathlib import Path

from .models import Report


def _money(v: float | None, symbol: str) -> str:
    if v is None:
        return "n/a"
    if float(v).is_integer():
        return f"{symbol}{int(v):,}"
    return f"{symbol}{v:,.2f}"


def _symbol_for(currency: str) -> str:
    return {"EUR": "€", "USD": "$", "GBP": "£"}.get(currency, currency + " ")


def _plan_line(plan, currency: str) -> str:
    cap = plan.capacity_label()
    period = plan.period_label()
    cap_label = plan.data_cap_label()
    bits = [f"{cap} data"]
    if period and period != "—":
        bits.append(period)
    if cap_label:
        bits.append(cap_label)
    return " · ".join(bits)


def render_console(report: Report) -> str:
    it = report.itinerary
    sym = _symbol_for(it.currency)
    lines: list[str] = []
    w = lines.append
    w("eSIM recommendation")
    w("=" * 78)
    w(
        f"Itinerary: {', '.join(it.names)}  |  {it.days} day(s)  |  "
        f"~{it.data_gb:g} GB data  |  currency {it.currency}"
    )
    w("")

    t = report.totals
    w(
        f"Scanned {t['unique_plans_scanned']} unique plans across {t['destinations']} "
        f"destination(s); {t['pool_covering_all_destinations']} cover every destination."
    )
    w(
        f"Excluded {t['speed_cap_excluded']} speed-capped plan(s), "
        f"{t['unpurchasable_excluded']} unpurchasable(s). "
        f"{t['eligible_candidates']} eligible, {t['meeting_data_need']} meet the data need."
    )
    w("")

    if report.winners:
        w(f"Top picks (currency {it.currency}, USD in parentheses where different):")
        w("-" * 78)
        for r in report.winners:
            p = r.plan
            w(f"{r.rank}. {p.name}  —  {p.provider}")
            price = _money(r.display_price, _symbol_for(r.currency))
            usd = f"  (US${_money(r.usd_price, '')})" if (
                r.currency != "USD" and r.usd_price is not None
            ) else ""
            w(f"   price: {price}{usd}   ·   {_plan_line(p, r.currency)}")
            ppg = f"   ·   US${r.price_per_gb}/GB" if r.price_per_gb else ""
            flag = "" if r.meets_data_need else "   [below your data need]"
            w(f"   coverage: {r.coverage.label}{flag}{ppg}")
            for reason in r.reasons:
                w(f"      • {reason}")
            for caveat in r.caveats:
                w(f"      ⚠ {caveat}")
            w(f"      {r.url}")
            w("")
    else:
        w("No single plan satisfied the filters. See per-destination fallback below.")
        w("")

    if report.fallback:
        w("Per-destination fallback (no whole-itinerary winner):")
        for f in report.fallback:
            w(f"   {f['destination']}: {f['plan']} — {f['provider']} (US${f['usd_price']})")
        w("")

    w("-" * 78)
    w("Excluded speed-capped plans (sample, first 50 of the total):")
    if report.excluded_speed_cap:
        for e in report.excluded_speed_cap[:10]:
            why = "; ".join(e["reasons"])
            w(f"   ✗ {e['name']} — {e['provider']}: {why}")
        if len(report.excluded_speed_cap) > 10:
            w(f"   … {len(report.excluded_speed_cap) - 10} more")
    else:
        w("   (none)")
    w("")

    if report.unverified_coverage:
        w("Plans with ambiguous/uncertain name coverage (still verified by site listing):")
        for u in report.unverified_coverage[:10]:
            w(f"   ? {u['name']} — {u['provider']}: {u['label']}")
        if len(report.unverified_coverage) > 10:
            w(f"   … {len(report.unverified_coverage) - 10} more")
        w("")

    w("Provenance / notes:")
    for n in report.notes:
        w(f"   • {n}")
    w(f"   generated {report.generated_at}")
    w("")
    w("Snapshots (raw responses cached for reproducibility):")
    for url, m in report.snapshot_manifest.items():
        short = url.replace("https://esimdb.com", "")
        w(f"   {m.get('source','?')}: {short}  sha256={ (m.get('sha256') or '')[:12] }…")
    w("")
    return "\n".join(lines)


def render_markdown(report: Report) -> str:
    it = report.itinerary
    t = report.totals
    md: list[str] = []
    w = md.append
    w("# eSIM Recommendation")
    w("")
    w(f"- **Itinerary:** {', '.join(it.names)}")
    w(f"- **Duration:** {it.days} day(s)")
    w(f"- **Data need:** ~{it.data_gb:g} GB")
    w(f"- **Currency:** {it.currency}")
    w(f"- **Generated:** {report.generated_at}")
    w("")
    w("## Summary")
    w("")
    w(
        f"Scanned **{t['unique_plans_scanned']}** unique plans across "
        f"**{t['destinations']}** destination(s); **{t['pool_covering_all_destinations']}** "
        f"cover every destination. Excluded **{t['speed_cap_excluded']}** speed-capped and "
        f"**{t['unpurchasable_excluded']}** unpurchasable plan(s). "
        f"**{t['meeting_data_need']}** of **{t['eligible_candidates']}** eligible plans meet the data need."
    )
    w("")
    if report.winners:
        w("## Recommendations")
        w("")
        w("| # | Plan | Provider | Price | Data / validity | Coverage | Meets need |")
        w("|---|------|----------|-------|-----------------|----------|------------|")
        for r in report.winners:
            price = _money(r.display_price, _symbol_for(r.currency))
            ppg = f" (US${r.price_per_gb}/GB)" if r.price_per_gb else ""
            meets = "yes" if r.meets_data_need else "no"
            w(
                f"| {r.rank} | {r.plan.name} | {r.plan.provider} | {price}{ppg} | "
                f"{_plan_line(r.plan, r.currency)} | {r.coverage.label} | {meets} |"
            )
        w("")
        for r in report.winners:
            w(f"### {r.rank}. {r.plan.name} — {r.plan.provider}")
            for reason in r.reasons:
                w(f"- {reason}")
            for caveat in r.caveats:
                w(f"- ⚠ {caveat}")
            w(f"- URL: {r.url}")
            w("")
    else:
        w("## Recommendations")
        w("")
        w("No single plan satisfied the filters. Per-destination fallback:")
        w("")
        for f in report.fallback:
            w(f"- **{f['destination']}:** {f['plan']} — {f['provider']} (US${f['usd_price']})")
        w("")
    w("## Excluded speed-capped plans (sample)")
    w("")
    if report.excluded_speed_cap:
        for e in report.excluded_speed_cap[:50]:
            w(f"- **{e['name']}** — {e['provider']}: {'; '.join(e['reasons'])}")
        if len(report.excluded_speed_cap) > 50:
            w(f"- … {len(report.excluded_speed_cap) - 50} more")
    else:
        w("- (none)")
    w("")
    w("## Ambiguous name coverage")
    w("")
    if report.unverified_coverage:
        for u in report.unverified_coverage[:50]:
            w(f"- **{u['name']}** — {u['provider']}: {u['label']}")
    else:
        w("- (none)")
    w("")
    w("## Provenance & notes")
    w("")
    for n in report.notes:
        w(f"- {n}")
    w("")
    w("### Snapshots")
    w("")
    for url, m in report.snapshot_manifest.items():
        w(f"- `{m.get('source','?')}` {url} — sha256 `{(m.get('sha256') or '')[:12]}…`")
    w("")
    return "\n".join(md)


def write_report(report: Report, out_path: Path | None, fmt: str = "console") -> None:
    ext = {"markdown": "md", "json": "json", "console": "txt"}.get(fmt, "txt")
    if out_path is None:
        out_path = Path("report." + ext)
    if fmt == "json":
        out_path.write_text(json.dumps(report.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")
    elif fmt == "markdown":
        out_path.write_text(render_markdown(report), encoding="utf-8")
    else:
        out_path.write_text(render_console(report), encoding="utf-8")


def render_json(report: Report) -> str:
    return json.dumps(report.to_dict(), indent=2, ensure_ascii=False)
