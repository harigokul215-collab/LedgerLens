from __future__ import annotations

import uuid
from datetime import date, datetime

import pandas as pd
import streamlit as st

from core import ai, config, db, pipeline
from views import ui

st.title("Scan receipts")
queue: list[dict] = st.session_state.setdefault("queue", [])
st.session_state.setdefault("uploader_key", 0)
cats = db.categories()
currencies = list(config.CURRENCIES)
default_cur = db.get_setting("default_currency", "USD")


def enqueue(name: str, raw: bytes) -> None:
    try:
        d = pipeline.process(name, raw)
    except Exception as exc:  # unreadable file
        st.error(f"**{name}**: couldn't read this file ({exc}).")
        return
    d["uid"] = uuid.uuid4().hex[:10]
    d["name"] = name
    queue.append(d)


# ---------------------------------------------------------------- inputs
ai_on = ai.available()
t_upload, t_camera, t_quick = st.tabs(["📤 Upload", "📷 Camera", "⚡ Quick add" if ai_on else "✍️ Manual entry"])

with t_upload:
    files = st.file_uploader(
        "Drop receipt photos or PDFs. Batch uploads welcome.",
        type=["png", "jpg", "jpeg", "webp", "pdf"], accept_multiple_files=True,
        key=f"up_{st.session_state['uploader_key']}",
    )
    if files and st.button(f"Extract {len(files)} receipt{'s' if len(files) > 1 else ''}", type="primary", icon="✨"):
        with st.status(f"Reading {len(files)} receipt(s)…", expanded=True) as status:
            for i, f in enumerate(files, start=1):
                status.update(label=f"Reading {f.name} ({i}/{len(files)})…")
                enqueue(f.name, f.getvalue())
            status.update(label="Done. Review below.", state="complete", expanded=False)
        st.session_state["uploader_key"] += 1
        st.rerun()

with t_camera:
    shot = st.camera_input("Hold the receipt flat, fill the frame, avoid glare.")
    if shot is not None and st.button("Use this photo", type="primary", icon="✨"):
        with st.spinner("Reading receipt…"):
            enqueue(f"camera-{datetime.now():%H%M%S}.jpg", shot.getvalue())
        st.rerun()

with t_quick:
    if ai_on:
        st.caption("Describe an expense in plain words. Claude fills in the fields for you to check.")
        c1, c2 = st.columns([4, 1], vertical_alignment="bottom")
        text = c1.text_input("Describe it", placeholder="Lunch with Priya at Truffles ₹1,450 yesterday, paid UPI",
                             label_visibility="collapsed")
        if c2.button("Add", type="primary", width="stretch", disabled=not text):
            try:
                with st.spinner("Understanding…"):
                    d = pipeline.quick(text)
                d["uid"], d["name"] = uuid.uuid4().hex[:10], "Quick add"
                queue.append(d)
                st.rerun()
            except ai.AIUnavailable as exc:
                st.warning(f"{exc} Use the blank form instead.")
    else:
        st.caption("Record an expense that has no receipt, such as cash, a transfer or a lost bill.")
    if st.button("Blank form (no receipt)", icon="✍️", type="secondary" if ai_on else "primary"):
        d = pipeline.build_draft({"currency": default_cur}, "manual", [], None)
        d["warnings"] = []
        d["uid"], d["name"] = uuid.uuid4().hex[:10], "Manual entry"
        queue.append(d)
        st.rerun()

if not queue:
    reader = ("Claude reads merchant, date, totals, tax, line items and suggests a category." if ai_on else
              "OCR reads the merchant, date, total, tax and line items, and guesses a category.")
    st.info(f"Receipts you scan appear here for a quick check before they're saved. {reader}", icon="🧾")
    st.stop()

# ---------------------------------------------------------------- review queue
st.divider()
head = st.container()
collected: dict[str, tuple[dict, list[dict]]] = {}


def remove(uid: str) -> None:
    st.session_state["queue"] = [d for d in st.session_state["queue"] if d["uid"] != uid]


def field(uid: str, name: str):
    return st.session_state.get(f"{uid}:{name}")


for d in queue:
    uid = d["uid"]
    rf = d.get("file")
    with st.container(border=True):
        top = st.columns([3, 2])
        top[0].markdown(f"**{d['name']}**  \n<span class='rt-sub'>{ui.source_badge(d['source'], d.get('confidence'))}</span>",
                        unsafe_allow_html=True)
        if d.get("learned"):
            top[1].caption("🧠 Category remembered from your earlier receipts")
        for w in d.get("warnings", []):
            st.warning(ui.md(w), icon="⚠️")
        for dup in d.get("duplicates", []):
            st.error(ui.md(f"Possible duplicate of #{dup['id']}: {dup['merchant']} · {dup['purchase_date']} · "
                           f"{config.money(dup['total'], dup['currency'])} ({dup['reason']})."), icon="👯")

        img_col, form_col = st.columns([1, 1.7], gap="large") if rf else (None, st.container())
        if rf:
            img_col.image(rf.image, width="stretch")

        with form_col:
            a, b, c = st.columns([2.2, 1.2, 1])
            a.text_input("Merchant", value=d["merchant"], key=f"{uid}:merchant")
            try:
                dval = datetime.strptime(d["purchase_date"], "%Y-%m-%d").date()
            except (TypeError, ValueError):
                dval = date.today()
            b.date_input("Date", value=dval, key=f"{uid}:date", max_value=date.today())
            c.selectbox("Currency", currencies, index=currencies.index(d["currency"]) if d["currency"] in currencies else 0,
                        key=f"{uid}:currency")

            a, b, c = st.columns([1.2, 1.6, 1.2])
            a.number_input("Total", value=float(d["total"] or 0), min_value=0.0, step=0.01, format="%.2f",
                           key=f"{uid}:total")
            b.selectbox("Category", cats, index=cats.index(d["category"]) if d["category"] in cats else len(cats) - 1,
                        key=f"{uid}:category")
            pm = config.PAYMENT_METHODS
            c.selectbox("Paid with", pm, index=pm.index(d["payment_method"]) if d["payment_method"] in pm else len(pm) - 1,
                        key=f"{uid}:payment", format_func=lambda x: x or "—")

            a, b = st.columns([2, 1.4])
            a.text_input("Purpose", value=d["purpose"], key=f"{uid}:purpose", placeholder="e.g. Client dinner")
            b.text_input("Tags", value=d.get("tags", ""), key=f"{uid}:tags", placeholder="comma, separated")

            a, b, c = st.columns(3)
            a.toggle("Business expense", value=d.get("is_business", False), key=f"{uid}:biz")
            b.toggle("Reimbursable", value=d.get("reimbursable", False), key=f"{uid}:reimb")

            with st.expander("Tax, tip, line items & notes"):
                a, b, c = st.columns(3)
                a.number_input("Subtotal", value=d["subtotal"], min_value=0.0, step=0.01, format="%.2f", key=f"{uid}:subtotal")
                b.number_input("Tax", value=d["tax"], min_value=0.0, step=0.01, format="%.2f", key=f"{uid}:tax")
                c.number_input("Tip", value=d["tip"], min_value=0.0, step=0.01, format="%.2f", key=f"{uid}:tip")
                items_df = pd.DataFrame(d["line_items"] or [], columns=["description", "quantity", "unit_price", "amount"])
                edited = st.data_editor(
                    items_df, num_rows="dynamic", hide_index=True, width="stretch", key=f"{uid}:items",
                    column_config={
                        "description": st.column_config.TextColumn("Item", width="large"),
                        "quantity": st.column_config.NumberColumn("Qty", format="%g"),
                        "unit_price": st.column_config.NumberColumn("Unit price", format="%.2f"),
                        "amount": st.column_config.NumberColumn("Amount", format="%.2f"),
                    },
                )
                item_sum = pd.to_numeric(edited["amount"], errors="coerce").sum()
                total_now = field(uid, "total") or 0
                if item_sum and total_now and abs(item_sum - total_now) > max(0.02 * total_now, 0.05):
                    sub = field(uid, "subtotal")
                    note = f" (subtotal {sub:,.2f})" if sub else ""
                    st.caption(f"Items add up to {item_sum:,.2f} vs total {total_now:,.2f}{note}. "
                               "The gap is usually tax, service charge or discounts.")
                st.text_area("Notes", value=d.get("notes", ""), key=f"{uid}:notes", height=68)
                if d.get("raw_text"):
                    st.caption("What was read from the receipt")
                    st.code(d["raw_text"][:4000], language=None)

            fields = {
                "merchant": (field(uid, "merchant") or "").strip(),
                "purchase_date": field(uid, "date").isoformat(),
                "total": float(field(uid, "total") or 0),
                "currency": field(uid, "currency"),
                "category": field(uid, "category"),
                "payment_method": field(uid, "payment") or "",
                "purpose": field(uid, "purpose") or "",
                "tags": ", ".join(t.strip() for t in (field(uid, "tags") or "").split(",") if t.strip()),
                "is_business": field(uid, "biz"),
                "reimbursable": field(uid, "reimb"),
                "reimburse_status": "pending" if field(uid, "reimb") else "n/a",
                "subtotal": field(uid, "subtotal"),
                "tax": field(uid, "tax"),
                "tip": field(uid, "tip"),
                "notes": field(uid, "notes") or "",
                "raw_text": d.get("raw_text", ""),
                "source": d["source"],
                "confidence": d.get("confidence"),
            }
            items = edited.dropna(how="all").to_dict("records")
            collected[uid] = (fields, items)

            s1, s2, _ = st.columns([1, 1.1, 2.5])
            if s1.button("Save", key=f"{uid}:save", type="primary", icon="💾", width="stretch",
                         disabled=fields["total"] <= 0 or not fields["merchant"]):
                pipeline.save(fields, items, rf)
                remove(uid)
                st.toast(f"Saved {fields['merchant']} · {config.money(fields['total'], fields['currency'])}", icon="✅")
                st.rerun()
            if s2.button("Discard", key=f"{uid}:discard", icon="🗑️", width="stretch"):
                remove(uid)
                st.rerun()

with head:
    ready = [u for u, (f, _) in collected.items() if f["total"] > 0 and f["merchant"]]
    c1, c2 = st.columns([3, 1], vertical_alignment="center")
    c1.subheader(f"Review {len(queue)} receipt{'s' if len(queue) != 1 else ''}")
    if len(queue) > 1 and c2.button(f"Save all {len(ready)} ready", type="primary", icon="💾", width="stretch",
                                    disabled=not ready):
        by_uid = {d["uid"]: d for d in queue}
        for u in ready:
            f, items = collected[u]
            pipeline.save(f, items, by_uid[u].get("file"))
            remove(u)
        st.toast(f"Saved {len(ready)} receipts", icon="✅")
        st.rerun()
