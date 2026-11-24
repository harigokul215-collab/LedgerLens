from __future__ import annotations

from datetime import date

import streamlit as st

from core import config, db, pipeline, reports
from views import ui

st.title("Reimbursements")
base = ui.base()
df = db.receipts_df()
claims = df[df["reimbursable"]] if not df.empty else df

if claims.empty:
    st.info("Mark receipts as **Reimbursable** when scanning (or in Receipts) and they'll gather here, "
            "ready to send as one PDF report with every receipt image attached.", icon="💼")
    st.stop()

by = claims.groupby("reimburse_status")["total_base"].agg(["sum", "count"])
c1, c2, c3 = st.columns(3)
for col, status, label in [(c1, "pending", "To claim"), (c2, "submitted", "Submitted, awaiting payment"),
                           (c3, "reimbursed", "Reimbursed")]:
    s = by.loc[status] if status in by.index else None
    col.metric(label, ui.money(s["sum"] if s is not None else 0),
               delta=f"{int(s['count']) if s is not None else 0} receipts", delta_color="off", delta_arrow="off",
               border=True)

status = st.segmented_control("Show", ["pending", "submitted", "reimbursed"], default="pending",
                              format_func=str.capitalize) or "pending"
rows = claims[claims["reimburse_status"] == status].sort_values("date")
if rows.empty:
    st.caption(f"No {status} claims.")
    st.stop()

table = rows[["id", "purchase_date", "merchant", "category", "purpose", "total", "currency", "total_base", "tags"]].copy()
table.insert(0, "include", True)
table["Amount"] = [config.money(t, c) for t, c in zip(table["total"], table["currency"])]
edited = st.data_editor(
    table, hide_index=True, width="stretch", key=f"claims_{status}_{st.session_state.get('claims_v', 0)}",
    disabled=[c for c in table.columns if c != "include"],
    column_order=["include", "purchase_date", "merchant", "category", "purpose", "Amount", "total_base", "tags"],
    column_config={"include": st.column_config.CheckboxColumn("✓"),
                   "purchase_date": "Date", "merchant": "Merchant", "category": "Category", "purpose": "Purpose",
                   "total_base": st.column_config.NumberColumn(base, format="%.2f"), "tags": "Tags"},
)
chosen = rows[rows["id"].isin(edited.loc[edited["include"], "id"])]
st.markdown(ui.md(f"**{len(chosen)} selected · {ui.money(chosen['total_base'].sum())}**"))

with st.container(border=True):
    st.markdown("#### Build the claim")
    a, b = st.columns(2)
    title = a.text_input("Report title", value=f"Expense claim · {date.today():%B %Y}")
    who = b.text_input("Submitted by", placeholder="Your name (optional)")
    c1, c2, c3 = st.columns(3)
    if c1.button("Generate PDF", type="primary", icon="📄", disabled=chosen.empty, width="stretch"):
        with st.spinner("Building PDF with receipt images…"):
            st.session_state["claim_pdf"] = reports.expense_report_pdf(chosen, base, title, who)
    if c2.button("Generate ZIP (PDF + CSV + images)", icon="🗜️", disabled=chosen.empty, width="stretch"):
        with st.spinner("Bundling…"):
            st.session_state["claim_zip"] = reports.bundle_zip(chosen, base, title, who)
    if st.session_state.get("claim_pdf"):
        st.download_button("⬇️ Download PDF", st.session_state["claim_pdf"], "expense-claim.pdf", "application/pdf")
    if st.session_state.get("claim_zip"):
        st.download_button("⬇️ Download ZIP", st.session_state["claim_zip"], "expense-claim.zip", "application/zip")

    next_state = {"pending": "submitted", "submitted": "reimbursed", "reimbursed": None}[status]
    if next_state and c3.button(f"Mark selected as {next_state}", icon="✅", disabled=chosen.empty, width="stretch"):
        for i in chosen["id"]:
            pipeline.update(int(i), {"reimburse_status": next_state})
        st.session_state["claims_v"] = st.session_state.get("claims_v", 0) + 1
        st.session_state.pop("claim_pdf", None)
        st.session_state.pop("claim_zip", None)
        st.toast(f"{len(chosen)} receipt(s) marked {next_state}", icon="✅")
        st.rerun()
