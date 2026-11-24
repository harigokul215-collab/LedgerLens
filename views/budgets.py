from __future__ import annotations

from datetime import date

import pandas as pd
import streamlit as st

from core import db, insights
from views import ui

st.title("Budgets")
base = ui.base()
df = db.receipts_df()
cats = db.categories()
limits = db.budgets()
today = date.today()

st.caption(f"Monthly limits in {base}. Each bar's tick marks where you'd be if you spent evenly through the month.")

lines = insights.budget_status(df, limits, today) if not df.empty else []
if lines:
    total_limit = sum(b.limit for b in lines)
    total_spent = sum(b.spent for b in lines)
    c1, c2, c3 = st.columns(3)
    c1.metric("Budgeted", ui.money(total_limit), border=True)
    c2.metric("Spent so far", ui.money(total_spent), delta=f"{total_spent / total_limit:.0%} used",
              delta_color="off", delta_arrow="off", border=True)
    left_total = total_limit - total_spent
    per_day = left_total / max(1, insights.month_bounds(today)[1].day - today.day + 1)
    c3.metric("Left to spend", ui.money(left_total),
              delta=f"{ui.money(per_day)} per day" if left_total > 0 else "Over the total budget",
              delta_color="off" if left_total > 0 else "inverse", delta_arrow="off", border=True)

    cols = st.columns(2, gap="large")
    for i, b in enumerate(lines):
        with cols[i % 2], st.container(border=True):
            h1, h2 = st.columns([2, 1])
            h1.markdown(f"**{b.category}**")
            with h2:
                ui.status_badge(b.status)
            ui.progress(b.spent, b.limit, b.pace_pct, b.status)
            left = b.limit - b.spent
            line = (f"{ui.money(b.spent)} of {ui.money(b.limit)} · "
                    f"{'over by ' + ui.money(-left) if left < 0 else ui.money(left) + ' left'} · "
                    f"on pace for {ui.money(b.projected)}")
            st.markdown(f"<span class='rt-sub'>{ui._h(line)}</span>", unsafe_allow_html=True)
else:
    st.info("No budgets yet. Set some below. Suggested amounts are based on your recent months.", icon="🎯")

# ---------------------------------------------------------------- editor
st.subheader("Set monthly limits")
hist = insights.monthly_by_category(df, 4, today).iloc[:-1] if not df.empty else pd.DataFrame()
avg3 = hist.mean() if not hist.empty else pd.Series(dtype=float)
table = pd.DataFrame({
    "category": cats,
    "avg_3m": [round(float(avg3.get(c, 0)), 2) for c in cats],
    "limit": [limits.get(c) for c in cats],
})
edited = st.data_editor(
    table, hide_index=True, width="stretch", key="budget_editor",
    disabled=["category", "avg_3m"],
    column_config={
        "category": st.column_config.TextColumn("Category"),
        "avg_3m": st.column_config.NumberColumn(f"Avg last 3 months ({base})", format="%.0f"),
        "limit": st.column_config.NumberColumn(f"Monthly limit ({base})", min_value=0, format="%.0f"),
    },
)
c1, c2, _ = st.columns([1, 1.6, 3])
if c1.button("Save budgets", type="primary", icon="💾"):
    db.set_budgets({r["category"]: float(r["limit"]) for r in edited.to_dict("records")
                    if pd.notna(r["limit"]) and r["limit"]})
    st.toast("Budgets saved", icon="🎯")
    st.rerun()
if c2.button("Suggest from my history", icon="🪄", disabled=avg3.empty,
             help="Sets each limit to your 3-month average, trimmed 5% and rounded."):
    step = 100 if base in ("INR", "JPY", "KRW", "IDR", "HUF") else 10
    db.set_budgets({c: max(step, round(v * 0.95 / step) * step) for c, v in avg3.items() if v > 0})
    st.rerun()

# ---------------------------------------------------------------- history
if limits and not df.empty:
    st.subheader("Last six months against budget")
    six = insights.monthly_by_category(df, 6, today)
    rows = []
    for cat, lim in limits.items():
        row = {"Category": cat, "Limit": ui.money(lim)}
        for m, v in six.get(cat, pd.Series(0, index=six.index)).items():
            mark = "🔴" if v > lim else "🟢"
            row[m.strftime("%b")] = f"{mark} {ui.money(v)}"
        rows.append(row)
    st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
