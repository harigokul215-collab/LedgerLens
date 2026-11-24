from __future__ import annotations

from datetime import date

import pandas as pd
import streamlit as st

from core import ai, config, db, insights
from views import charts, ui

st.title("Insights")
base = ui.base()
df = db.receipts_df()
today = date.today()

if df.empty:
    ui.empty_state("Insights appear once you have receipts")
    st.stop()

ai_on = ai.available()
tab_names = ["🔁 Subscriptions", "🔍 Unusual charges", "📊 Category shifts", "🧾 Tax & business"]
tabs = st.tabs(tab_names + (["✨ AI monthly review"] if ai_on else []))
t_subs, t_unusual, t_shift, t_tax = tabs[:4]


def ai_review() -> None:
    st.caption("Claude reads this month's numbers (not your receipts' images) and writes a short review.")
    if st.button(f"Write my {today:%B} review", type="primary", icon="✨"):
        try:
            with st.spinner("Analysing your month…"):
                facts = insights.review_facts(df, db.budgets(), base, today)
                st.session_state["review"] = (today.isoformat(), ai.monthly_review(facts, base))
        except ai.AIUnavailable as exc:
            st.warning(str(exc))
    if st.session_state.get("review"):
        when, text = st.session_state["review"]
        with st.container(border=True):
            st.markdown(ui.md(text))
            st.caption(f"Generated {when}")


if ai_on:  # only shown once a Claude API key is configured
    with tabs[4]:
        ai_review()

# ---------------------------------------------------------------- subscriptions
with t_subs:
    rec = insights.recurring(df, today)
    if rec.empty:
        st.info("No recurring charges detected yet. It takes three charges at a steady rhythm.", icon="🔁")
    else:
        active = rec[rec["active"]]
        c1, c2, c3 = st.columns(3)
        c1.metric("Active subscriptions", len(active), border=True)
        c2.metric("Per month", ui.money(active["annual_cost"].sum() / 12), border=True)
        c3.metric("Per year", ui.money(active["annual_cost"].sum()), border=True)
        show = rec.copy()
        show["Amount"] = [config.money(a, c) for a, c in zip(show["native_amount"], show["currency"])]
        show["Per year"] = show["annual_cost"].map(ui.money)
        show["Status"] = show["active"].map({True: "Active", False: "Lapsed?"})
        show["Price"] = [f"▲ {p:.0f}%" if p > 0 else "" for p in show["price_change_pct"]]
        st.dataframe(
            show[["merchant", "category", "cadence", "Amount", "Price", "Per year", "last_charge", "next_expected",
                  "charges", "Status"]].rename(columns={
                "merchant": "Merchant", "category": "Category", "cadence": "Every", "last_charge": "Last charge",
                "next_expected": "Next expected", "charges": "Charges seen"}),
            hide_index=True, width="stretch")
        if len(active):
            st.markdown("**Annual cost of each subscription**")
            a = active.sort_values("annual_cost", ascending=False)
            charts.show(charts.ranked_bars(a["merchant"].tolist(), a["annual_cost"].tolist(), base), key="subs")

# ---------------------------------------------------------------- anomalies
with t_unusual:
    an = insights.anomalies(df, lookback_days=120, today=today)
    st.caption("Receipts from the last four months that are far above what you usually spend in that category.")
    if an.empty:
        st.success("Nothing unusual. Every recent receipt is in line with your habits.", icon="✅")
    else:
        for r in an.itertuples():
            ui.card("warning", "🔍", f"{r.merchant}: {ui.money(r.total_base)} on {r.date:%d %b %Y}",
                    f"{r.times_typical:.1f}× a typical {r.category} receipt ({ui.money(r.typical)}). Receipt #{r.id}.")

# ---------------------------------------------------------------- shifts
with t_shift:
    sh = insights.category_shifts(df, today)
    sh = sh[(sh["this_month"] > 0) | (sh["baseline"] > 0)]
    st.caption(f"{today:%B} so far vs the same days of the previous three months, on average. "
               "Orange = spending more than usual, blue = less.")
    if sh.empty:
        st.info("Not enough history yet.")
    else:
        charts.show(charts.diverging_shift(sh, base), key="shift")
        tbl = sh.copy()
        tbl["This month"] = tbl["this_month"].map(ui.money)
        tbl["Typical by now"] = tbl["baseline"].map(ui.money)
        tbl["Change"] = [f"{p:+.0f}%" if pd.notna(p) else "new" for p in tbl["change_pct"]]
        st.dataframe(tbl[["category", "This month", "Typical by now", "Change"]].rename(columns={"category": "Category"}),
                     hide_index=True, width="stretch")

# ---------------------------------------------------------------- tax
with t_tax:
    years = sorted(df["date"].dt.year.dropna().unique().astype(int), reverse=True)
    year = st.selectbox("Year", years)
    t = insights.tax_summary(df, int(year))
    c1, c2, c3 = st.columns(3)
    c1.metric("Total spend", ui.money(t["total_spend"]), border=True)
    c2.metric("Business expenses", ui.money(t["business_spend"]), border=True)
    c3.metric("Awaiting reimbursement", ui.money(t["reimbursable_outstanding"]), border=True)
    l, r = st.columns(2, gap="large")
    with l:
        st.markdown("**Business expenses by category**")
        if t["business_by_category"].empty:
            st.caption("Mark receipts as business expenses to see them here.")
        else:
            colors = insights.category_color_map(db.categories())
            charts.show(charts.category_bars(t["business_by_category"], colors, base), key="biz")
    with r:
        st.markdown("**Tax paid, as recorded on receipts**")
        if t["tax_paid_native"].empty:
            st.caption("No tax amounts recorded.")
        else:
            st.dataframe(pd.DataFrame({"Currency": t["tax_paid_native"].index,
                                       "Tax": [config.money(v, c) for c, v in t["tax_paid_native"].items()]}),
                         hide_index=True, width="stretch")
        st.caption("Export the year's business receipts as a PDF with images from the Receipts page "
                   "(filter: Business). Not tax advice.")
