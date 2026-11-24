"""Analytics that run locally on the ledger: no AI needed.

All amounts are in the base currency (``total_base``).
"""
from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date as Date, timedelta

import numpy as np
import pandas as pd

from . import config


def _today(today: Date | None) -> Date:
    return today or Date.today()


def month_bounds(d: Date) -> tuple[Date, Date]:
    return d.replace(day=1), d.replace(day=calendar.monthrange(d.year, d.month)[1])


def prev_month(d: Date) -> Date:
    return (d.replace(day=1) - timedelta(days=1)).replace(day=1)


def in_range(df: pd.DataFrame, start: Date, end: Date) -> pd.DataFrame:
    return df[(df["date"] >= pd.Timestamp(start)) & (df["date"] <= pd.Timestamp(end))]


# ------------------------------------------------------------------ recurring
CADENCES = [("Weekly", 7, 2), ("Monthly", 30.4, 4), ("Quarterly", 91, 8), ("Yearly", 365, 15)]


def recurring(df: pd.DataFrame, today: Date | None = None) -> pd.DataFrame:
    """Detect subscriptions: >= 3 charges from one merchant at a steady cadence and amount."""
    today = _today(today)
    rows = []
    for key, g in df[df["merchant_key"] != ""].groupby("merchant_key"):
        g = g.sort_values("date")
        if len(g) < 3:
            continue
        gaps = g["date"].diff().dt.days.dropna()
        median_gap = float(gaps.median())
        amounts = g["total_base"]
        native = g["total"]  # judge price stability in the charge's own currency, free of FX noise
        cv = float(native.std(ddof=0) / native.mean()) if native.mean() else 1.0
        for name, period, tol in CADENCES:
            if abs(median_gap - period) <= tol and (gaps.sub(period).abs() <= tol * 2).mean() >= 0.7 and cv <= 0.2:
                last = g["date"].iloc[-1].date()
                nxt = last + timedelta(days=round(period))
                change, change_pct = _price_change(native)
                rows.append({
                    "merchant": g["merchant"].iloc[-1],
                    "merchant_key": key,
                    "category": g["category"].mode().iloc[0],
                    "cadence": name,
                    "typical_amount": float(amounts.tail(3).median()),
                    "currency": g["currency"].iloc[-1],
                    "native_amount": float(g["total"].iloc[-1]),
                    "charges": len(g),
                    "last_charge": last,
                    "next_expected": nxt,
                    "annual_cost": float(amounts.tail(3).median() * 365 / period),
                    "active": (today - last).days <= period + tol * 3,
                    "price_change": change,          # native currency
                    "price_change_pct": change_pct,
                })
                break
    out = pd.DataFrame(rows)
    return out.sort_values("annual_cost", ascending=False) if not out.empty else out


def _price_change(native: pd.Series) -> tuple[float, float]:
    """A fixed price that recently stepped up: earlier charges flat, then a new higher price
    within the last two charges. Variable bills (electricity) don't qualify."""
    vals = native.round(2).tolist()
    latest = vals[-1]
    tail = 0
    while tail < len(vals) and abs(vals[-1 - tail] - latest) <= 0.005 * latest:
        tail += 1
    earlier = vals[: len(vals) - tail]
    if not earlier or tail > 2 or len(earlier) < 2:
        return 0.0, 0.0
    prev = pd.Series(earlier)
    if prev.std(ddof=0) / prev.mean() > 0.01 or latest <= prev.iloc[-1] * 1.01:
        return 0.0, 0.0
    return float(latest - prev.iloc[-1]), float((latest / prev.iloc[-1] - 1) * 100)


# ------------------------------------------------------------------ anomalies
def anomalies(df: pd.DataFrame, lookback_days: int = 60, today: Date | None = None) -> pd.DataFrame:
    """Recent receipts far above that category's usual amount (robust z-score on log amounts)."""
    today = _today(today)
    flagged = []
    recent_cut = pd.Timestamp(today - timedelta(days=lookback_days))
    for cat, g in df[df["total_base"] > 0].groupby("category"):
        if len(g) < 6:
            continue
        logs = np.log(g["total_base"])
        med = logs.median()
        mad = (logs - med).abs().median() * 1.4826 or 1e-9
        z = (logs - med) / mad
        typical = float(np.exp(med))
        for idx in g.index[(z > 3.0) & (g["date"] >= recent_cut)]:
            r = g.loc[idx]
            flagged.append({
                "id": int(r["id"]), "date": r["date"].date(), "merchant": r["merchant"],
                "category": cat, "total_base": float(r["total_base"]),
                "typical": typical, "times_typical": float(r["total_base"] / typical),
            })
    out = pd.DataFrame(flagged)
    return out.sort_values("times_typical", ascending=False) if not out.empty else out


# ------------------------------------------------------------------ month view
def month_summary(df: pd.DataFrame, today: Date | None = None) -> dict:
    today = _today(today)
    start, end = month_bounds(today)
    days_in = end.day
    elapsed = today.day
    this = in_range(df, start, today)
    pstart = prev_month(today)
    pend_same = pstart + timedelta(days=min(elapsed, calendar.monthrange(pstart.year, pstart.month)[1]) - 1)
    prev_same = in_range(df, pstart, pend_same)
    prev_full = in_range(df, *month_bounds(pstart))

    mtd = float(this["total_base"].sum())
    rec = recurring(df, today)
    upcoming = 0.0
    recurring_this_month = 0.0
    if not rec.empty:
        active = rec[rec["active"]]
        due = active[(active["next_expected"] > today) & (active["next_expected"] <= end)]
        upcoming = float(due["typical_amount"].sum())
        keys = set(active["merchant_key"])
        recurring_this_month = float(this[this["merchant_key"].isin(keys)]["total_base"].sum())
    discretionary_rate = (mtd - recurring_this_month) / max(elapsed, 1)
    projected = mtd + discretionary_rate * (days_in - elapsed) + upcoming

    prev_same_total = float(prev_same["total_base"].sum())
    return {
        "month_label": today.strftime("%B %Y"),
        "mtd": mtd,
        "receipts": int(len(this)),
        "daily_avg": mtd / max(elapsed, 1),
        "prev_same_period": prev_same_total,
        "prev_month_total": float(prev_full["total_base"].sum()),
        "delta_pct": (mtd - prev_same_total) / prev_same_total * 100 if prev_same_total else None,
        "projected": projected,
        "upcoming_recurring": upcoming,
        "days_left": days_in - elapsed,
        "top_category": this.groupby("category")["total_base"].sum().idxmax() if not this.empty else None,
    }


@dataclass
class BudgetLine:
    category: str
    limit: float
    spent: float
    projected: float
    pct: float
    pace_pct: float
    status: str  # good | warning | critical


def budget_status(df: pd.DataFrame, budgets: dict[str, float], today: Date | None = None) -> list[BudgetLine]:
    today = _today(today)
    start, end = month_bounds(today)
    this = in_range(df, start, today)
    spent_by = this.groupby("category")["total_base"].sum()
    pace = today.day / end.day
    lines = []
    for cat, limit in budgets.items():
        if limit <= 0:
            continue
        spent = float(spent_by.get(cat, 0.0))
        projected = spent / pace if pace else spent
        pct = spent / limit
        status = "critical" if pct >= 1 else "warning" if projected > limit * 1.05 else "good"
        lines.append(BudgetLine(cat, limit, spent, projected, pct, pace, status))
    return sorted(lines, key=lambda b: -b.pct)


def category_shifts(df: pd.DataFrame, today: Date | None = None, min_amount: float = 0.0) -> pd.DataFrame:
    """This month (to date) vs the same days of the previous three months, per category."""
    today = _today(today)
    this = in_range(df, today.replace(day=1), today).groupby("category")["total_base"].sum()
    hist = []
    m = today
    for _ in range(3):
        m = prev_month(m)
        last_day = min(today.day, calendar.monthrange(m.year, m.month)[1])
        hist.append(in_range(df, m, m.replace(day=last_day)).groupby("category")["total_base"].sum())
    baseline = pd.concat(hist, axis=1).fillna(0).mean(axis=1) if hist else pd.Series(dtype=float)
    cats = sorted(set(this.index) | set(baseline.index))
    out = pd.DataFrame({
        "category": cats,
        "this_month": [float(this.get(c, 0)) for c in cats],
        "baseline": [float(baseline.get(c, 0)) for c in cats],
    })
    out["change"] = out["this_month"] - out["baseline"]
    out["change_pct"] = np.where(out["baseline"] > 0, out["change"] / out["baseline"] * 100, np.nan)
    out = out[(out["this_month"] >= min_amount) | (out["baseline"] >= min_amount)]
    return out.sort_values("change", ascending=False)


def monthly_by_category(df: pd.DataFrame, months: int = 12, today: Date | None = None) -> pd.DataFrame:
    today = _today(today)
    start = today.replace(day=1)
    for _ in range(months - 1):
        start = prev_month(start)
    sub = in_range(df, start, today).copy()
    sub["month"] = sub["date"].dt.to_period("M").dt.to_timestamp()
    pivot = sub.pivot_table(index="month", columns="category", values="total_base", aggfunc="sum", fill_value=0)
    full = pd.date_range(start, today.replace(day=1), freq="MS")
    return pivot.reindex(full, fill_value=0)


def weekday_profile(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame(columns=["weekday", "avg_per_day"])
    days = df.groupby(df["date"].dt.normalize())["total_base"].sum()
    span = pd.date_range(days.index.min(), days.index.max(), freq="D")
    days = days.reindex(span, fill_value=0)
    prof = days.groupby(days.index.dayofweek).mean()
    names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    return pd.DataFrame({"weekday": [names[i] for i in prof.index], "avg_per_day": prof.values})


def top_merchants(df: pd.DataFrame, n: int = 10) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame(columns=["merchant", "total", "visits", "avg"])
    g = df.groupby("merchant_key").agg(
        merchant=("merchant", "last"), total=("total_base", "sum"), visits=("id", "count"))
    g["avg"] = g["total"] / g["visits"]
    return g.sort_values("total", ascending=False).head(n).reset_index(drop=True)


def tax_summary(df: pd.DataFrame, year: int) -> dict:
    yr = df[df["date"].dt.year == year]
    biz = yr[yr["is_business"]]
    return {
        "year": year,
        "total_spend": float(yr["total_base"].sum()),
        "business_spend": float(biz["total_base"].sum()),
        "business_by_category": biz.groupby("category")["total_base"].sum().sort_values(ascending=False),
        "tax_paid_native": yr.dropna(subset=["tax"]).groupby("currency")["tax"].sum(),
        "reimbursable_outstanding": float(
            yr[yr["reimbursable"] & yr["reimburse_status"].isin(["pending", "submitted"])]["total_base"].sum()
        ),
    }


def category_color_map(categories: list[str]) -> dict[str, str]:
    """Fixed colour per category by its position in the user's category list (never by rank)."""
    colors = {}
    slot = 0
    for c in categories:
        if c == "Other":
            continue
        if slot < len(config.SERIES) - 1:  # keep the last slot free; extra categories fold into Other
            colors[c] = config.SERIES[slot]
            slot += 1
    colors["Other"] = config.OTHER_GRAY
    return colors


def fold_categories(pivot: pd.DataFrame, colors: dict[str, str]) -> pd.DataFrame:
    keep = [c for c in pivot.columns if c in colors and c != "Other"]
    rest = [c for c in pivot.columns if c not in keep]
    out = pivot[keep].copy()
    if rest:
        out["Other"] = pivot[rest].sum(axis=1)
    return out


# ------------------------------------------------------------------ smart feed
def feed(df: pd.DataFrame, budgets: dict[str, float], base: str, today: Date | None = None) -> list[dict]:
    """Prioritised, plain-language observations for the dashboard."""
    today = _today(today)
    cards: list[dict] = []
    fmt = lambda v: config.money(v, base)  # noqa: E731

    for b in budget_status(df, budgets, today):
        if b.status == "critical":
            cards.append({"level": "critical", "icon": "🚨", "title": f"{b.category} budget exceeded",
                          "body": f"{fmt(b.spent)} spent of {fmt(b.limit)} ({b.pct:.0%})."})
        elif b.status == "warning":
            cards.append({"level": "warning", "icon": "⚠️", "title": f"{b.category} is running hot",
                          "body": f"On pace for {fmt(b.projected)} against a {fmt(b.limit)} budget."})

    for r in anomalies(df, lookback_days=30, today=today).head(3).itertuples():
        cards.append({"level": "warning", "icon": "🔍", "title": f"Unusual charge at {r.merchant}",
                      "body": f"{fmt(r.total_base)} on {r.date:%d %b}, about {r.times_typical:.1f}× a typical "
                              f"{r.category} receipt ({fmt(r.typical)})."})

    rec = recurring(df, today)
    if not rec.empty:
        hikes = rec[(rec["active"]) & (rec["price_change"] > 0)]
        for r in hikes.head(2).itertuples():
            cards.append({"level": "warning", "icon": "📈", "title": f"{r.merchant} raised its price",
                          "body": f"Now {config.money(r.native_amount, r.currency)}, up "
                                  f"{config.money(r.price_change, r.currency)} ({r.price_change_pct:.0f}%). "
                                  f"That's about {fmt(r.annual_cost)} a year."})
        soon = rec[(rec["active"]) & (rec["next_expected"] >= today) &
                   (rec["next_expected"] <= today + timedelta(days=7))]
        for r in soon.head(3).itertuples():
            cards.append({"level": "info", "icon": "🔁", "title": f"{r.merchant} renews soon",
                          "body": f"About {fmt(r.typical_amount)} expected around {r.next_expected:%d %b}."})
        active = rec[rec["active"]]
        if len(active):
            cards.append({"level": "info", "icon": "📦", "title": f"{len(active)} active subscriptions",
                          "body": f"Costing about {fmt(active['annual_cost'].sum())} a year."})

    shifts = category_shifts(df, today)
    if not shifts.empty:
        big = shifts[(shifts["baseline"] > 0) & (shifts["change_pct"] >= 40) &
                     (shifts["change"] >= max(shifts["baseline"].sum() * 0.03, 1))]
        for r in big.head(2).itertuples():
            ratio = r.this_month / r.baseline
            label = f"{ratio:.1f}× usual" if ratio >= 3 else f"up {r.change_pct:.0f}%"
            cards.append({"level": "info", "icon": "📊", "title": f"{r.category} {label}",
                          "body": f"{fmt(r.this_month)} so far vs {fmt(r.baseline)} by this point in a typical month."})
        drops = shifts[(shifts["baseline"] > 0) & (shifts["this_month"] > 0) & (shifts["change_pct"] <= -40)
                       & (today.day >= 10)]
        for r in drops.head(1).itertuples():
            cards.append({"level": "good", "icon": "✅", "title": f"{r.category} down {abs(r.change_pct):.0f}%",
                          "body": f"{fmt(r.this_month)} vs a typical {fmt(r.baseline)} by now. Nice."})

    pending = df[df["reimbursable"] & (df["reimburse_status"] == "pending")]
    if len(pending):
        cards.append({"level": "info", "icon": "💼", "title": f"{len(pending)} receipts awaiting reimbursement",
                      "body": f"{fmt(pending['total_base'].sum())} you can claim back. Generate a report on the Reimbursements page."})

    order = {"critical": 0, "warning": 1, "info": 2, "good": 3}
    return sorted(cards, key=lambda c: order[c["level"]])


def review_facts(df: pd.DataFrame, budgets: dict[str, float], base: str, today: Date | None = None) -> dict:
    """Compact JSON-able facts for the AI monthly review."""
    today = _today(today)
    summary = month_summary(df, today)
    shifts = category_shifts(df, today)
    rec = recurring(df, today)
    return {
        "base_currency": base,
        "summary": summary,
        "by_category_this_month": shifts[["category", "this_month", "baseline", "change_pct"]]
        .round(2).to_dict("records"),
        "budgets": [b.__dict__ for b in budget_status(df, budgets, today)],
        "top_merchants_this_month": top_merchants(in_range(df, today.replace(day=1), today), 5)
        .round(2).to_dict("records"),
        "subscriptions": rec[["merchant", "cadence", "typical_amount", "annual_cost", "active"]]
        .round(2).to_dict("records") if not rec.empty else [],
        "unusual_charges": anomalies(df, 31, today).round(2).to_dict("records"),
    }
