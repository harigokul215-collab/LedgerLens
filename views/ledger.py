from __future__ import annotations

from datetime import date, timedelta

import pandas as pd
import streamlit as st

from core import config, db, imaging, pipeline, reports
from views import ui

st.title("Receipts")
base = ui.base()
df = db.receipts_df()
cats = db.categories()

if df.empty:
    ui.empty_state("No receipts yet")
    st.stop()

# ---------------------------------------------------------------- filters
f1, f2, f3, f4 = st.columns([2, 1.5, 1.6, 2.3], vertical_alignment="center")
q = f1.text_input("Search", placeholder="Merchant, purpose, notes, tags or item…", label_visibility="collapsed")
dmin, dmax = df["date"].min().date(), max(df["date"].max().date(), date.today())
default_start = max(dmin, date.today() - timedelta(days=90))
rng = f2.date_input("Dates", value=(default_start, dmax), min_value=dmin, max_value=dmax, label_visibility="collapsed")
sel_cats = f3.multiselect("Categories", cats, placeholder="All categories", label_visibility="collapsed")
flags = f4.pills("Flags", ["Business", "Reimbursable", "Estimated FX"], selection_mode="multi",
                 label_visibility="collapsed")

view = df
if isinstance(rng, tuple) and len(rng) == 2:
    view = view[(view["date"] >= pd.Timestamp(rng[0])) & (view["date"] <= pd.Timestamp(rng[1]))]
if sel_cats:
    view = view[view["category"].isin(sel_cats)]
if q:
    ql = q.lower()
    items = db.all_line_items()
    item_hits = set(items[items["description"].str.lower().str.contains(ql, regex=False, na=False)]["receipt_id"])
    text = (view["merchant"] + " " + view["purpose"] + " " + view["notes"] + " " + view["tags"]).str.lower()
    view = view[text.str.contains(ql, regex=False) | view["id"].isin(item_hits)]
if "Business" in (flags or []):
    view = view[view["is_business"]]
if "Reimbursable" in (flags or []):
    view = view[view["reimbursable"]]
if "Estimated FX" in (flags or []):
    view = view[view["fx_estimated"]]

m1, m2, m3, m4 = st.columns(4)
m1.metric("Receipts", len(view), border=True)
m2.metric(f"Total ({base})", ui.money(view["total_base"].sum()), border=True)
m3.metric("Average", ui.money(view["total_base"].mean() if len(view) else 0), border=True)
m4.metric("Tax recorded", ", ".join(config.money(v, c) for c, v in view.dropna(subset=["tax"]).groupby("currency")["tax"].sum().items()) or "—", border=True)

# ---------------------------------------------------------------- editable grid
editable = view[["id", "purchase_date", "merchant", "total", "currency", "total_base", "category", "purpose",
                 "tags", "payment_method", "is_business", "reimbursable", "reimburse_status", "source"]].copy()
editable["purchase_date"] = pd.to_datetime(editable["purchase_date"]).dt.date
editable.insert(0, "select", False)
editable["has_image"] = view["image_path"].ne("").map({True: "📎", False: ""})

st.caption("Edit cells directly, then **Save changes**. Tick rows for bulk actions.")
edited = st.data_editor(
    editable, hide_index=True, width="stretch", height=min(560, 38 + 35 * len(editable)),
    key=f"grid_{st.session_state.get('grid_v', 0)}",
    disabled=["id", "total_base", "source", "has_image"],
    column_order=["select", "has_image", "purchase_date", "merchant", "total", "currency", "total_base", "category",
                  "purpose", "tags", "payment_method", "is_business", "reimbursable", "reimburse_status", "source"],
    column_config={
        "select": st.column_config.CheckboxColumn("✓", width="small"),
        "has_image": st.column_config.TextColumn("", width="small"),
        "purchase_date": st.column_config.DateColumn("Date", format="DD MMM YYYY"),
        "merchant": st.column_config.TextColumn("Merchant", width="medium"),
        "total": st.column_config.NumberColumn("Amount", format="%.2f"),
        "currency": st.column_config.SelectboxColumn("Cur", options=list(config.CURRENCIES), width="small"),
        "total_base": st.column_config.NumberColumn(base, format="%.2f"),
        "category": st.column_config.SelectboxColumn("Category", options=cats),
        "purpose": st.column_config.TextColumn("Purpose", width="medium"),
        "tags": st.column_config.TextColumn("Tags"),
        "payment_method": st.column_config.SelectboxColumn("Paid with", options=config.PAYMENT_METHODS),
        "is_business": st.column_config.CheckboxColumn("Biz"),
        "reimbursable": st.column_config.CheckboxColumn("Claim"),
        "reimburse_status": st.column_config.SelectboxColumn("Claim status", options=config.REIMBURSE_STATES),
        "source": st.column_config.TextColumn("Source", width="small"),
    },
)

compare_cols = ["purchase_date", "merchant", "total", "currency", "category", "purpose", "tags",
                "payment_method", "is_business", "reimbursable", "reimburse_status"]
orig = editable.set_index("id")[compare_cols]
new = edited.set_index("id")[compare_cols]
changed = [i for i in new.index if list(map(str, new.loc[i])) != list(map(str, orig.loc[i]))]
selected = edited.loc[edited["select"], "id"].astype(int).tolist()

b1, b2, b3, b4, b5 = st.columns([1.3, 1.4, 1.6, 1.4, 2])
if b1.button(f"Save changes ({len(changed)})", type="primary", disabled=not changed, icon="💾", width="stretch"):
    for i in changed:
        row = new.loc[i].to_dict()
        row["purchase_date"] = pd.Timestamp(row["purchase_date"]).date().isoformat()
        row["is_business"], row["reimbursable"] = bool(row["is_business"]), bool(row["reimbursable"])
        if row["reimbursable"] and row["reimburse_status"] == "n/a":
            row["reimburse_status"] = "pending"
        pipeline.update(int(i), row)
    st.session_state["grid_v"] = st.session_state.get("grid_v", 0) + 1
    st.toast(f"Updated {len(changed)} receipt(s)", icon="✅")
    st.rerun()

with b2.popover(f"Bulk edit ({len(selected)})", disabled=not selected, icon="🗂️", width="stretch"):
    new_cat = st.selectbox("Set category", ["—"] + cats)
    new_status = st.selectbox("Set claim status", ["—"] + config.REIMBURSE_STATES)
    add_tag = st.text_input("Add tag")
    if st.button("Apply to selected", type="primary"):
        for i in selected:
            upd = {}
            if new_cat != "—":
                upd["category"] = new_cat
            if new_status != "—":
                upd["reimburse_status"] = new_status
                upd["reimbursable"] = new_status != "n/a"
            if add_tag.strip():
                cur = db.get_receipt(i)["tags"]
                tags = [t.strip() for t in cur.split(",") if t.strip()]
                if add_tag.strip() not in tags:
                    tags.append(add_tag.strip())
                upd["tags"] = ", ".join(tags)
            if upd:
                pipeline.update(i, upd)
        st.session_state["grid_v"] = st.session_state.get("grid_v", 0) + 1
        st.rerun()

with b3.popover(f"Delete selected ({len(selected)})", disabled=not selected, icon="🗑️", width="stretch"):
    st.write(f"Permanently delete {len(selected)} receipt(s) and their images?")
    if st.button("Yes, delete", type="primary"):
        db.delete_receipts(selected)
        st.session_state["grid_v"] = st.session_state.get("grid_v", 0) + 1
        st.toast(f"Deleted {len(selected)} receipt(s)", icon="🗑️")
        st.rerun()

with b4.popover("Export", icon="📥", width="stretch"):
    st.download_button("CSV", reports.to_csv(view), "receipts.csv", "text/csv", width="stretch")
    st.download_button("Excel workbook", reports.to_excel(view, base), "receipts.xlsx",
                       "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", width="stretch")
    if st.button("Build PDF report", width="stretch"):
        st.session_state["ledger_pdf"] = reports.expense_report_pdf(view, base, "Expense report", include_images=True)
    if st.session_state.get("ledger_pdf"):
        st.download_button("Download PDF", st.session_state["ledger_pdf"], "expense-report.pdf", "application/pdf",
                           width="stretch")

# ---------------------------------------------------------------- detail view
st.divider()
st.subheader("Receipt detail")
options = view["id"].tolist()
if not options:
    st.caption("No receipts match the filters.")
    st.stop()
labels = {int(r.id): f"{r.purchase_date} · {r.merchant} · {config.money(r.total, r.currency)}" for r in view.itertuples()}
pick = st.selectbox("Open receipt", options, format_func=lambda i: labels[int(i)], label_visibility="collapsed",
                    index=options.index(selected[0]) if selected and selected[0] in options else 0)
r = db.get_receipt(int(pick))
img_col, info_col = st.columns([1, 1.5], gap="large")
with img_col:
    path = imaging.stored_path(r["image_path"])
    if path:
        st.image(str(path), width="stretch")
    else:
        st.caption("No image stored for this receipt.")
with info_col:
    st.markdown(f"### {r['merchant'] or 'Unknown merchant'}")
    st.markdown(f"<div class='rt-hero'>{config.money(r['total'], r['currency'])}</div>", unsafe_allow_html=True)
    if r["currency"] != r["base_currency"]:
        est = " (estimated rate)" if r["fx_estimated"] else ""
        st.caption(f"≈ {config.money(r['total_base'], r['base_currency'])} at {r['fx_rate']:.4f}{est}")
    st.write(f"**{r['purchase_date']}** · {r['category']} · {r['payment_method'] or 'payment n/a'} · "
             f"{ui.source_badge(r['source'], r['confidence'])}")
    if r["purpose"]:
        st.write(f"**Purpose:** {r['purpose']}")
    parts = [f"Subtotal {r['subtotal']:,.2f}" if r["subtotal"] else "", f"Tax {r['tax']:,.2f}" if r["tax"] else "",
             f"Tip {r['tip']:,.2f}" if r["tip"] else ""]
    if any(parts):
        st.caption(" · ".join(p for p in parts if p))
    if r["tags"]:
        st.write(" ".join(f"`{t.strip()}`" for t in r["tags"].split(",") if t.strip()))
    if r["notes"]:
        st.info(r["notes"])
    items = pd.DataFrame(db.line_items(r["id"]))
    if not items.empty:
        st.dataframe(items.rename(columns={"description": "Item", "quantity": "Qty", "unit_price": "Unit",
                                           "amount": "Amount"}), hide_index=True, width="stretch")
    if r["raw_text"]:
        with st.expander("Extracted text"):
            st.code(r["raw_text"][:5000], language=None)
