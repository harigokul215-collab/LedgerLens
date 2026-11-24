"""LedgerLens: entry point. Run with `streamlit run app.py`."""
from __future__ import annotations

import streamlit as st

st.set_page_config(page_title="LedgerLens", page_icon="🧾", layout="wide")

from core import ai, db, fx  # noqa: E402
from views import ui  # noqa: E402


@st.cache_resource
def _boot() -> bool:
    db.init_db()
    return True


_boot()
ui.inject_css()
ai.set_session_key(st.session_state.get("api_key"))

# Fix any receipts converted with estimated rates (offline) or an old base currency,
# once per browser session.
if not st.session_state.get("_repriced"):
    st.session_state["_repriced"] = True
    try:
        fx.reprice()
    except Exception:
        pass

ai_on = ai.available()

plan = [
    st.Page("views/budgets.py", title="Budgets", icon="🎯"),
    st.Page("views/insights.py", title="Insights", icon="💡"),
    st.Page("views/reimburse.py", title="Reimbursements", icon="💼"),
]
if ai_on:  # AI-only pages appear once a Claude API key is configured
    plan.append(st.Page("views/assistant.py", title="Ask your receipts", icon="✨"))

pages = {
    "": [
        st.Page("views/dashboard.py", title="Dashboard", icon="📊", default=True),
        st.Page("views/scan.py", title="Scan receipts", icon="📷"),
        st.Page("views/ledger.py", title="Receipts", icon="🧾"),
    ],
    "Plan & analyse": plan,
    "App": [st.Page("views/settings.py", title="Settings", icon="⚙️")],
}

nav = st.navigation(pages)

with st.sidebar:
    st.markdown("### 🧾 LedgerLens")
    if ai_on:
        st.caption(f"✨ Claude extraction on · {ai.status()[1]}")
    else:
        st.caption("🔎 Offline mode (Tesseract OCR)")
    st.caption(f"Reporting currency: **{ui.base()}**")

nav.run()
