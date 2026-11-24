from __future__ import annotations

import streamlit as st

from core import ai, assistant, db
from views import charts, ui

st.title("Ask your receipts")
base = ui.base()
st.caption("Ask anything about your spending. Claude queries your local database (read-only) and can draw charts.")

st.session_state.setdefault("chat_api", [])   # raw API messages, replayed each turn
st.session_state.setdefault("chat_view", [])  # what we render: {role, text, charts, queries}

SUGGESTIONS = [
    "How much did I spend on dining this month vs last month?",
    "What are my top 5 merchants this year?",
    "Show my monthly spending trend as a chart",
    "Which subscriptions cost me the most per year?",
    "How much is waiting to be reimbursed?",
    "What did I buy at Whole Foods most often?",
]


def render(entry: dict, idx: int) -> None:
    with st.chat_message(entry["role"], avatar="🧑" if entry["role"] == "user" else "✨"):
        st.markdown(ui.md(entry["text"]))
        for j, c in enumerate(entry.get("charts", [])):
            st.markdown(f"**{c['title']}**")
            charts.show(charts.simple(c["df"], c["type"], c["x"], c["y"]), key=f"chat_{idx}_{j}")
        if entry.get("queries"):
            with st.expander(f"{len(entry['queries'])} quer{'y' if len(entry['queries']) == 1 else 'ies'} run"):
                for q in entry["queries"]:
                    st.code(q, language="sql")


if db.receipts_df().empty:
    st.info("Add some receipts (or demo data from Settings) first.", icon="🧾")

for i, e in enumerate(st.session_state["chat_view"]):
    render(e, i)

prompt = st.chat_input("e.g. How much did I spend on Uber in October?")
if not st.session_state["chat_view"]:
    cols = st.columns(3)
    for i, s in enumerate(SUGGESTIONS):
        if cols[i % 3].button(s, key=f"sug{i}", width="stretch"):
            prompt = s

if prompt:
    user = {"role": "user", "text": prompt}
    st.session_state["chat_view"].append(user)
    render(user, len(st.session_state["chat_view"]))
    with st.chat_message("assistant", avatar="✨"):
        status = st.empty()
        try:
            history, turn = assistant.ask(
                st.session_state["chat_api"], prompt, base, db.categories(),
                on_status=lambda s: status.caption(s))
            st.session_state["chat_api"] = history
            entry = {"role": "assistant", "text": turn.text or "_(no answer)_", "charts": turn.charts,
                     "queries": turn.queries}
        except ai.AIUnavailable as exc:
            entry = {"role": "assistant", "text": f"⚠️ {exc}"}
        status.empty()
    st.session_state["chat_view"].append(entry)
    st.rerun()

if st.session_state["chat_view"]:
    if st.button("New conversation", icon="🧹"):
        st.session_state["chat_api"] = []
        st.session_state["chat_view"] = []
        st.rerun()
