from __future__ import annotations

import io

import pandas as pd
import streamlit as st

from core import ai, config, db, demo, fx, ocr, pipeline, reports
from views import ui

st.title("Settings")
currencies = list(config.CURRENCIES)

t_general, t_ai, t_cats, t_import, t_data = st.tabs(
    ["💱 Currency", "✨ AI (optional)", "🏷️ Categories & rules", "📥 Import", "🗄️ Data"])

# ---------------------------------------------------------------- currency
with t_general:
    base = ui.base()
    c1, c2 = st.columns(2)
    new_base = c1.selectbox("Reporting currency", currencies, index=currencies.index(base),
                            help="Every receipt is converted to this at the exchange rate on its purchase date.")
    default_cur = db.get_setting("default_currency", "USD")
    new_default = c2.selectbox("Default currency for new receipts", currencies, index=currencies.index(default_cur))
    live = st.toggle("Fetch historical exchange rates online (ECB reference rates via frankfurter.dev)",
                     value=db.get_setting("live_fx", True),
                     help="Only currency codes and dates are sent. Off = built-in approximate rates.")
    if st.button("Save currency settings", type="primary"):
        db.set_setting("default_currency", new_default)
        db.set_setting("live_fx", live)
        if new_base != base:
            db.set_setting("base_currency", new_base)
            with st.spinner(f"Converting every receipt to {new_base}…"):
                n, est = fx.reprice()
            st.success(f"Re-priced {n} receipts in {new_base}." + (f" {est} use estimated rates." if est else ""))
        else:
            st.toast("Saved", icon="✅")
    if st.button("Refresh all exchange rates", icon="🔄"):
        with st.spinner("Re-pricing…"):
            n, est = fx.reprice(force=True)
        st.success(f"Re-priced {n} receipts." + (f" {est} still estimated (offline?)." if est else ""))

# ---------------------------------------------------------------- AI
with t_ai:
    enabled = st.toggle("Use Claude to read receipts", value=db.get_setting("ai_enabled", True),
                        help="Off = offline Tesseract OCR only.")
    ok, why = ai.status()
    (st.success if ok else st.warning)(why)
    key = st.text_input("Anthropic API key (optional)", type="password", value=st.session_state.get("api_key", ""),
                        help="Kept in memory for this browser session only, never written to disk. "
                             "For a permanent setup set ANTHROPIC_API_KEY or run `ant auth login`.")
    effort = st.select_slider("Extraction effort", ["low", "medium", "high"], value=db.get_setting("ai_effort", "medium"),
                              help="Higher reads tricky or handwritten receipts more carefully but is slower.")
    st.caption(f"Model: `{config.AI_MODEL}`. If a request is declined, the API automatically retries it on a fallback model.")
    if st.button("Save AI settings", type="primary"):
        st.session_state["api_key"] = key
        ai.set_session_key(key)
        db.set_setting("ai_enabled", enabled)
        db.set_setting("ai_effort", effort)
        st.toast("Saved", icon="✅")
        st.rerun()
    st.divider()
    st.markdown("**Offline OCR**: " + ("✅ Tesseract found" if ocr.tesseract_available() else
                "❌ Tesseract not found. Install it for offline scanning (see README)."))

# ---------------------------------------------------------------- categories
with t_cats:
    st.markdown("**Categories**: one per line. The first seven get their own chart colour.")
    cats = db.categories()
    text = st.text_area("Categories", "\n".join(cats), height=260, label_visibility="collapsed")
    if st.button("Save categories", type="primary"):
        new = list(dict.fromkeys(c.strip() for c in text.splitlines() if c.strip()))
        if "Other" not in new:
            new.append("Other")
        removed = set(cats) - set(new)
        df = db.receipts_df()
        moved = df[df["category"].isin(removed)]["id"].tolist()
        for i in moved:
            db.update_receipt(int(i), {"category": "Other"})
        db.set_setting("categories", new)
        st.success(f"Saved {len(new)} categories." + (f" {len(moved)} receipts moved to Other." if moved else ""))

    st.divider()
    st.markdown("**Merchant rules**: learned automatically each time you save a receipt. "
                "New receipts from these merchants get the category (and purpose) below.")
    rules = db.merchant_rules_df()
    if rules.empty:
        st.caption("No rules learned yet.")
    else:
        edited = st.data_editor(
            rules.drop(columns=["merchant_key"]), num_rows="dynamic", hide_index=True, width="stretch",
            column_config={"display_name": "Merchant", "category": st.column_config.SelectboxColumn("Category", options=cats),
                           "purpose": "Default purpose", "hits": st.column_config.NumberColumn("Times seen", disabled=True)})
        a, b = st.columns([1, 2])
        if a.button("Save rules"):
            db.replace_merchant_rules(edited)
            st.toast("Rules saved", icon="🧠")
        if b.button("Apply rules to existing receipts",
                    help="Re-categorise every saved receipt whose merchant has a rule."):
            df = db.receipts_df()
            lookup = {db.merchant_key(r["display_name"]): r for r in edited.to_dict("records")}
            n = 0
            for r in df.itertuples():
                rule = lookup.get(r.merchant_key)
                if rule and rule["category"] != r.category:
                    db.update_receipt(int(r.id), {"category": rule["category"]})
                    n += 1
            st.success(f"Updated {n} receipts.")

# ---------------------------------------------------------------- import
with t_import:
    st.markdown("**Import a bank or card statement (CSV)**")
    up = st.file_uploader("CSV file", type=["csv"], key="csv_import")
    if up is not None:
        raw = up.getvalue()
        try:
            preview = pd.read_csv(io.BytesIO(raw), nrows=200)
        except Exception as exc:
            st.error(f"Couldn't read CSV: {exc}")
            preview = None
        if preview is not None:
            st.dataframe(preview.head(5), width="stretch")
            cols = list(preview.columns)
            guess = lambda *names: next((c for c in cols if any(n in c.lower() for n in names)), cols[0])  # noqa: E731
            a, b, c = st.columns(3)
            mapping = {
                "merchant": a.selectbox("Merchant / description column", cols, index=cols.index(guess("merchant", "desc", "payee", "narration"))),
                "date": b.selectbox("Date column", cols, index=cols.index(guess("date"))),
                "amount": c.selectbox("Amount column", cols, index=cols.index(guess("amount", "debit", "value"))),
            }
            a, b, c = st.columns(3)
            cur_col = a.selectbox("Currency column", ["(use default)"] + cols)
            cat_col = b.selectbox("Category column", ["(auto from merchant rules)"] + cols)
            mapping["dayfirst"] = "yes" if c.toggle("Dates are day-first (31/12/2026)") else "no"
            if cur_col != "(use default)":
                mapping["currency"] = cur_col
            if cat_col != "(auto from merchant rules)":
                mapping["category"] = cat_col
            if st.button("Import rows", type="primary"):
                rows = reports.import_csv(raw, mapping, db.get_setting("default_currency", "USD"))
                cats = db.categories()
                n = 0
                for r in rows.to_dict("records"):
                    if r["total"] <= 0:
                        continue
                    rule = db.merchant_rule(r["merchant"])
                    cat = r["category"] if r.get("category") in cats else (rule["category"] if rule else "Other")
                    cur = r["currency"] if r["currency"] in config.CURRENCIES else db.get_setting("default_currency", "USD")
                    if db.find_duplicates("", r["merchant"], r["purchase_date"], r["total"]):
                        continue
                    pipeline.save({"merchant": r["merchant"], "purchase_date": r["purchase_date"],
                                   "total": r["total"], "currency": cur, "category": cat, "source": "import"})
                    n += 1
                st.success(f"Imported {n} transactions (duplicates skipped).")

# ---------------------------------------------------------------- data
with t_data:
    df = db.receipts_df()
    st.markdown(f"**{len(df)} receipts** stored in `{db.db_path()}`")
    if db.get_setting("legacy_import_count"):
        st.caption(f"Imported {db.get_setting('legacy_import_count')} records from the old USD/INR databases.")

    a, b = st.columns(2)
    with a, st.container(border=True):
        st.markdown("**Backup**: database plus every receipt image, in one ZIP.")
        if st.button("Prepare backup", icon="🗄️"):
            st.session_state["backup"] = reports.backup_zip()
        if st.session_state.get("backup"):
            st.download_button("⬇️ Download backup", st.session_state["backup"], "ledgerlens-backup.zip",
                               "application/zip")
    with b, st.container(border=True):
        st.markdown("**Restore**: replaces all current data with the backup.")
        bk = st.file_uploader("Backup ZIP", type=["zip"], key="restore", label_visibility="collapsed")
        if bk is not None and st.button("Restore now", type="primary"):
            try:
                n = reports.restore_zip(bk.getvalue())
                st.success(f"Restored {n} receipts.")
            except ValueError as exc:
                st.error(str(exc))

    with st.container(border=True):
        st.markdown("**Demo data**: seven months of realistic receipts in USD and INR, with subscriptions, "
                    "a business trip and an unusual purchase, so every feature has something to show.")
        n_demo = int((df["source"] == "demo").sum()) if not df.empty else 0
        c1, c2, _ = st.columns([1, 1, 2])
        if c1.button("Load demo data", disabled=n_demo > 0):
            with st.spinner("Generating…"):
                st.success(f"Added {demo.generate()} demo receipts.")
        if c2.button(f"Remove demo data ({n_demo})", disabled=n_demo == 0):
            st.success(f"Removed {demo.clear()} demo receipts.")

    with st.expander("Danger zone"):
        confirm = st.text_input("Type DELETE to erase every receipt and image")
        if st.button("Erase everything", disabled=confirm != "DELETE"):
            db.delete_receipts(df["id"].tolist())
            st.success("All receipts deleted.")
