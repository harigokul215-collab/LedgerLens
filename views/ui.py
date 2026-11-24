"""Shared UI helpers: styling, formatting, cards, empty states."""
from __future__ import annotations

import html

import streamlit as st

from core import config, db

CSS = """
<style>
.block-container { padding-top: 2.2rem; max-width: 1400px; }
[data-testid="stMetric"] { padding: .85rem 1rem; border-radius: .75rem;
  border: 1px solid rgba(128,128,128,.22); }
.rt-card { border: 1px solid rgba(128,128,128,.22); border-left-width: 4px; border-radius: .6rem;
  padding: .65rem .9rem; margin-bottom: .55rem; }
.rt-card .t { font-weight: 600; margin-bottom: .15rem; }
.rt-card .b { opacity: .82; font-size: .92rem; }
.rt-critical { border-left-color: #e34948; } .rt-warning { border-left-color: #eda100; }
.rt-info { border-left-color: #2a78d6; } .rt-good { border-left-color: #1baf7a; }
.rt-bar { position: relative; height: 10px; border-radius: 5px; background: rgba(128,128,128,.18); margin: .3rem 0 .2rem; }
.rt-bar > .fill { position: absolute; left: 0; top: 0; bottom: 0; border-radius: 5px; }
.rt-bar > .pace { position: absolute; top: -4px; bottom: -4px; width: 2px; background: currentColor; opacity: .55; }
.rt-sub { opacity: .7; font-size: .85rem; }
.rt-hero { font-size: 2.6rem; font-weight: 700; line-height: 1.1; letter-spacing: -.02em; }
</style>
"""


def inject_css() -> None:
    st.markdown(CSS, unsafe_allow_html=True)


def base() -> str:
    return db.get_setting("base_currency", "USD")


def money(v: float | None, cur: str | None = None) -> str:
    return config.money(v, cur or base())


def md(text: str) -> str:
    """Escape '$' so Streamlit doesn't render '$12 … $30' as LaTeX maths."""
    return (text or "").replace("\\$", "$").replace("$", "\\$")


def _h(text: str) -> str:
    return html.escape(text).replace("$", "&#36;")


def card(level: str, icon: str, title: str, body: str) -> None:
    st.markdown(
        f'<div class="rt-card rt-{level}"><div class="t">{icon} {_h(title)}</div>'
        f'<div class="b">{_h(body)}</div></div>',
        unsafe_allow_html=True,
    )


def progress(spent: float, limit: float, pace: float, status: str) -> None:
    """Budget bar with a tick showing where spending 'should' be by today."""
    pct = min(spent / limit, 1.0) if limit else 0
    color = config.STATUS["critical" if status == "critical" else "warning" if status == "warning" else "good"]
    st.markdown(
        f'<div class="rt-bar" title="{pct:.0%} used; tick = expected by today">'
        f'<div class="fill" style="width:{pct*100:.1f}%;background:{color}"></div>'
        f'<div class="pace" style="left:{min(pace,1)*100:.1f}%"></div></div>',
        unsafe_allow_html=True,
    )


STATUS_LABEL = {"good": ("On track", "green"), "warning": ("At risk", "orange"), "critical": ("Over budget", "red")}


def status_badge(status: str) -> None:
    label, color = STATUS_LABEL[status]
    st.badge(label, color=color, icon={"good": "✅", "warning": "⚠️", "critical": "🚨"}[status])


def empty_state(message: str) -> None:
    with st.container(border=True):
        st.markdown(f"#### {message}")
        st.write("Scan your first receipt, or load sample data to explore every feature.")
        c1, c2, _ = st.columns([1, 1, 3])
        if c1.button("📷 Scan a receipt", type="primary", width="stretch"):
            st.switch_page("views/scan.py")
        if c2.button("✨ Load demo data", width="stretch"):
            from core import demo

            with st.spinner("Generating seven months of realistic receipts…"):
                n = demo.generate()
            st.toast(f"Added {n} demo receipts. Remove them any time in Settings.", icon="✨")
            st.rerun()


def source_badge(source: str, confidence: float | None = None) -> str:
    label = {"ai": "✨ Claude", "ocr": "🔎 OCR", "manual": "✍️ Manual", "import": "📥 Import",
             "legacy": "🗄️ Legacy", "demo": "🧪 Demo"}.get(source, source)
    if confidence is not None:
        label += f" · {confidence:.0%} sure"
    return label
