"""Plotly chart builders with one consistent look.

Rules followed: category colour is fixed per category (never by rank), one
y-axis per chart, thin bars with rounded data ends, recessive grid, a legend
whenever there are 2+ series, and hover tooltips on every mark.
"""
from __future__ import annotations

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from core import config

FONT = dict(family="Inter, Segoe UI, system-ui, sans-serif", size=13)


def _layout(fig: go.Figure, height: int = 340, legend: bool = True, yfmt: str | None = None) -> go.Figure:
    fig.update_layout(
        height=height,
        margin=dict(l=8, r=8, t=12, b=8),
        font=FONT,
        showlegend=legend,
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="left", x=0, title=None),
        hoverlabel=dict(font_size=13),
        bargap=0.28,
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
    )
    fig.update_xaxes(showgrid=False, linecolor="rgba(128,128,128,.35)", ticks="")
    fig.update_yaxes(gridcolor="rgba(128,128,128,.16)", zeroline=False, tickformat=yfmt or "~s")
    return fig


def show(fig: go.Figure, key: str | None = None) -> None:
    st.plotly_chart(fig, width="stretch", theme="streamlit", key=key,
                    config={"displayModeBar": False})


def stacked_months(pivot: pd.DataFrame, colors: dict[str, str], base: str) -> go.Figure:
    sym = config.symbol(base)
    fig = go.Figure()
    order = [c for c in colors if c in pivot.columns]
    for cat in order:
        fig.add_bar(
            x=pivot.index, y=pivot[cat], name=cat, marker_color=colors[cat], marker_line_width=0,
            hovertemplate=f"<b>{cat}</b><br>%{{x|%b %Y}}: {sym}%{{y:,.0f}}<extra></extra>",
        )
    totals = pivot[order].sum(axis=1)
    fig.add_scatter(x=pivot.index, y=totals, mode="text", text=[f"{sym}{v:,.0f}" if v else "" for v in totals],
                    textposition="top center", textfont=dict(size=11, color="rgba(128,128,128,1)"),
                    showlegend=False, hoverinfo="skip")
    fig.update_layout(barmode="stack", legend_traceorder="normal")
    fig.update_xaxes(dtick="M1", tickformat="%b\n%Y")
    return _layout(fig, 360)


def ranked_bars(labels: list[str], values: list[float], base: str, color: str = config.SEQ_BLUE,
                extra: list[str] | None = None, height: int | None = None) -> go.Figure:
    """Horizontal ranked bars for a single measure (one series → one hue, no legend)."""
    sym = config.symbol(base)
    custom = extra or [""] * len(labels)
    fig = go.Figure(go.Bar(
        x=values[::-1], y=labels[::-1], orientation="h", marker_color=color, marker_cornerradius=4,
        text=[f"{sym}{v:,.0f}" for v in values[::-1]], textposition="outside", cliponaxis=False,
        customdata=custom[::-1],
        hovertemplate=f"<b>%{{y}}</b><br>{sym}%{{x:,.2f}}%{{customdata}}<extra></extra>",
    ))
    fig = _layout(fig, height or max(180, 34 * len(labels) + 40), legend=False)
    fig.update_xaxes(visible=False)
    fig.update_yaxes(gridcolor="rgba(0,0,0,0)")
    return fig


def category_bars(series: pd.Series, colors: dict[str, str], base: str) -> go.Figure:
    """Category totals, each bar in its category colour (identity), ranked by size."""
    s = series.sort_values(ascending=True)
    sym = config.symbol(base)
    total = s.sum() or 1
    fig = go.Figure(go.Bar(
        x=s.values, y=s.index, orientation="h", marker_cornerradius=4,
        marker_color=[colors.get(c, config.OTHER_GRAY) for c in s.index],
        text=[f"{sym}{v:,.0f}  ·  {v/total:.0%}" for v in s.values], textposition="outside", cliponaxis=False,
        hovertemplate=f"<b>%{{y}}</b><br>{sym}%{{x:,.2f}}<extra></extra>",
    ))
    fig = _layout(fig, max(200, 34 * len(s) + 40), legend=False)
    fig.update_xaxes(visible=False, range=[0, s.max() * 1.35 if len(s) else 1])
    return fig


def cumulative_vs_last(this: pd.Series, last: pd.Series, base: str, this_label: str, last_label: str,
                       projection: tuple[int, float, int, float] | None = None) -> go.Figure:
    """Running total by day of month: this month (solid), its projection (dashed), last month (dotted grey)."""
    sym = config.symbol(base)
    fig = go.Figure()
    fig.add_scatter(x=last.index, y=last.values, name=last_label, mode="lines",
                    line=dict(color=config.OTHER_GRAY, width=2, dash="dot"),
                    hovertemplate=f"Day %{{x}}: {sym}%{{y:,.0f}}<extra>{last_label}</extra>")
    if projection and projection[2] > projection[0]:
        d0, v0, d1, v1 = projection
        fig.add_scatter(x=[d0, d1], y=[v0, v1], name="Projected", mode="lines+markers",
                        line=dict(color=config.SEQ_BLUE, width=2, dash="dash"), marker=dict(size=[0, 9]),
                        hovertemplate=f"Day %{{x}}: {sym}%{{y:,.0f}}<extra>Projected</extra>")
    fig.add_scatter(x=this.index, y=this.values, name=this_label, mode="lines+markers",
                    line=dict(color=config.SEQ_BLUE, width=2.5), marker=dict(size=5),
                    hovertemplate=f"Day %{{x}}: {sym}%{{y:,.0f}}<extra>{this_label}</extra>")
    fig = _layout(fig, 300)
    fig.update_layout(hovermode="x unified")
    fig.update_xaxes(title=None, dtick=5, range=[0.5, 31.5])
    return fig


def weekday_bars(df: pd.DataFrame, base: str) -> go.Figure:
    sym = config.symbol(base)
    peak = df["avg_per_day"].max()
    fig = go.Figure(go.Bar(
        x=df["weekday"], y=df["avg_per_day"], marker_cornerradius=4,
        marker_color=[config.SEQ_BLUE if v == peak else "#9ec5f4" for v in df["avg_per_day"]],
        hovertemplate=f"<b>%{{x}}</b><br>Average {sym}%{{y:,.2f}} per day<extra></extra>",
    ))
    return _layout(fig, 260, legend=False)


def diverging_shift(df: pd.DataFrame, base: str) -> go.Figure:
    """Change vs typical month per category: orange = more than usual, blue = less."""
    sym = config.symbol(base)
    d = df.sort_values("change")
    fig = go.Figure(go.Bar(
        x=d["change"], y=d["category"], orientation="h", marker_cornerradius=4,
        marker_color=["#eb6834" if v > 0 else config.SEQ_BLUE for v in d["change"]],
        customdata=d[["this_month", "baseline"]].values,
        hovertemplate=(f"<b>%{{y}}</b><br>This month {sym}%{{customdata[0]:,.0f}}"
                       f"<br>Typical by now {sym}%{{customdata[1]:,.0f}}<br>Change {sym}%{{x:+,.0f}}<extra></extra>"),
    ))
    fig = _layout(fig, max(220, 32 * len(d) + 40), legend=False)
    fig.add_vline(x=0, line_color="rgba(128,128,128,.6)", line_width=1)
    return fig


def simple(df: pd.DataFrame, kind: str, x: str, y: str) -> go.Figure:
    """Single-series chart for the assistant."""
    if kind == "horizontal_bar":
        fig = go.Figure(go.Bar(x=df[y], y=df[x].astype(str), orientation="h", marker_color=config.SEQ_BLUE,
                               marker_cornerradius=4, hovertemplate="<b>%{y}</b><br>%{x:,.2f}<extra></extra>"))
        fig.update_yaxes(autorange="reversed")
        return _layout(fig, max(220, 30 * len(df) + 40), legend=False)
    if kind == "line":
        fig = go.Figure(go.Scatter(x=df[x], y=df[y], mode="lines+markers", line=dict(color=config.SEQ_BLUE, width=2.5),
                                   hovertemplate="%{x}<br>%{y:,.2f}<extra></extra>"))
        return _layout(fig, 300, legend=False)
    fig = go.Figure(go.Bar(x=df[x].astype(str), y=df[y], marker_color=config.SEQ_BLUE, marker_cornerradius=4,
                           hovertemplate="%{x}<br>%{y:,.2f}<extra></extra>"))
    return _layout(fig, 300, legend=False)
