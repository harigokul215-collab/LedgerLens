from __future__ import annotations

from datetime import date, timedelta

import pandas as pd
import streamlit as st

from core import db, insights
from views import charts, ui

base = ui.base()
df = db.receipts_df()
today = date.today()

st.title("Dashboard")

if df.empty:
    ui.empty_state("Nothing here yet")
    st.stop()

if df["fx_estimated"].any():
    st.caption(f"⚠️ {int(df['fx_estimated'].sum())} receipts use estimated exchange rates "
               "(rate service was offline). They'll be re-priced automatically.")

# ---------------------------------------------------------------- KPIs
s = insights.month_summary(df, today)
k1, k2, k3, k4 = st.columns(4)
k1.metric(
    f"Spent in {today:%B}", ui.money(s["mtd"]),
    delta=f"{s['delta_pct']:+.0f}% vs last month" if s["delta_pct"] is not None else None,
    delta_color="inverse", border=True,
    help=f"Compared with the first {today.day} days of last month ({ui.money(s['prev_same_period'])}).",
)
k2.metric("Projected month-end", ui.money(s["projected"]),
          help="Your day-to-day run rate for the rest of the month, plus subscriptions still due.",
          delta=f"Last month {ui.money(s['prev_month_total'])}", delta_color="off", delta_arrow="off", border=True)
k3.metric("Daily average", ui.money(s["daily_avg"]), delta=f"{s['days_left']} days left",
          delta_color="off", delta_arrow="off", border=True)
k4.metric("Receipts this month", s["receipts"], delta=f"Top: {s['top_category']}" if s["top_category"] else None,
          delta_color="off", delta_arrow="off", border=True)

# ---------------------------------------------------------------- feed + pace
left, right = st.columns([1.05, 1.6], gap="large")
with left:
    st.subheader("What needs your attention")
    cards = insights.feed(df, db.budgets(), base, today)
    if not cards:
        ui.card("good", "✅", "All clear", "No budget overruns, unusual charges or price hikes detected.")
    for c in cards[:5]:
        ui.card(c["level"], c["icon"], c["title"], c["body"])
    if len(cards) > 5:
        st.page_link("views/insights.py", label=f"{len(cards) - 5} more in Insights", icon="💡")

with right:
    st.subheader("This month vs last month")
    def running(start: date, end: date) -> pd.Series:
        sub = insights.in_range(df, start, end)
        by_day = sub.groupby(sub["date"].dt.day)["total_base"].sum()
        return by_day.reindex(range(1, end.day + 1), fill_value=0).cumsum()
    this = running(today.replace(day=1), today)
    pm = insights.prev_month(today)
    last = running(pm, insights.month_bounds(pm)[1])
    month_end = insights.month_bounds(today)[1].day
    charts.show(charts.cumulative_vs_last(this, last, base, f"{today:%B}", f"{pm:%B}",
                                          projection=(today.day, s["mtd"], month_end, s["projected"])), key="cum")
    st.caption("Running total by day of month. The dashed blue line runs to the projected month-end.")

# ---------------------------------------------------------------- trends
st.subheader("Spending by month")
colors = insights.category_color_map(db.categories())
pivot = insights.fold_categories(insights.monthly_by_category(df, 12, today), colors)
pivot = pivot.loc[:, (pivot.sum() > 0)]
charts.show(charts.stacked_months(pivot, colors, base), key="months")

period = st.segmented_control(
    "Period", ["This month", "Last month", "3 months", "This year", "12 months", "All time"],
    default="This month", label_visibility="collapsed",
) or "This month"
ranges = {
    "This month": (today.replace(day=1), today),
    "Last month": insights.month_bounds(pm),
    "3 months": (today - timedelta(days=91), today),
    "This year": (today.replace(month=1, day=1), today),
    "12 months": (today - timedelta(days=365), today),
    "All time": (df["date"].min().date(), today),
}
lo, hi = ranges[period]
sub = insights.in_range(df, lo, hi)

c1, c2 = st.columns(2, gap="large")
with c1:
    st.markdown(ui.md(f"**By category** · {ui.money(sub['total_base'].sum())}"))
    if sub.empty:
        st.caption("No receipts in this period.")
    else:
        charts.show(charts.category_bars(sub.groupby("category")["total_base"].sum(), colors, base), key="cats")
with c2:
    st.markdown("**Top merchants**")
    top = insights.top_merchants(sub, 8)
    if top.empty:
        st.caption("No receipts in this period.")
    else:
        extra = [f"<br>{v} visit{'s' if v != 1 else ''} · avg {ui.money(a)}" for v, a in zip(top["visits"], top["avg"])]
        charts.show(charts.ranked_bars(top["merchant"].tolist(), top["total"].tolist(), base, extra=extra), key="merch")

c3, c4 = st.columns([1, 1.3], gap="large")
with c3:
    st.markdown("**Which days you spend most** (average per day)")
    charts.show(charts.weekday_bars(insights.weekday_profile(df), base), key="wd")
with c4:
    st.markdown("**Latest receipts**")
    recent = df.head(8)[["purchase_date", "merchant", "category", "total", "currency", "total_base"]].copy()
    recent["Amount"] = [ui.money(t, c) for t, c in zip(recent["total"], recent["currency"])]
    recent[base] = recent["total_base"].map(ui.money)
    st.dataframe(recent[["purchase_date", "merchant", "category", "Amount", base]].rename(
        columns={"purchase_date": "Date", "merchant": "Merchant", "category": "Category"}),
        hide_index=True, width="stretch")
