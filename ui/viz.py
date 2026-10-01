"""Chart helpers (plotly). Colour follows the entity in a fixed, validated order (dataviz reference
palette slots 1–5, checked with validate_palette.js: all gates pass in light mode; aqua is under 3:1
contrast, so every chart ships a data table). One axis per chart, thin marks, recessive grid."""
from __future__ import annotations

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

INK, MUTED, GRID, SURFACE, DESK = "#1E2A38", "#5B6B7C", "#E3E8EE", "#FFFFFF", "#2C5E8C"
SLOTS = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4"]
# fixed entity → slot maps: a filter that removes one series never repaints the others
CHANNEL = {"Chat": SLOTS[0], "Form": SLOTS[1], "Phone": SLOTS[2], "Portal": SLOTS[3], "Email": SLOTS[4]}
OUTCOME = {"Bot-resolved": SLOTS[0], "Human-resolved": SLOTS[1], "Escalated (open)": SLOTS[2],
           "Bot in progress": SLOTS[3], "Cancelled / duplicate": SLOTS[4]}
WHO = {"Bot": SLOTS[0], "Human": SLOTS[1]}


def _layout(fig: go.Figure, title: str, height: int = 300, legend: bool = False) -> go.Figure:
    fig.update_layout(
        title={"text": title, "font": {"size": 15, "color": INK}, "x": 0, "xanchor": "left"},
        height=height, margin={"l": 8, "r": 8, "t": 44, "b": 8}, plot_bgcolor=SURFACE, paper_bgcolor=SURFACE,
        font={"family": "IBM Plex Sans, sans-serif", "color": MUTED, "size": 12}, showlegend=legend,
        legend={"orientation": "h", "y": -0.18, "x": 0, "title": None}, hoverlabel={"font_size": 12},
        bargap=0.35)
    fig.update_xaxes(showgrid=False, linecolor=GRID, ticks="")
    fig.update_yaxes(gridcolor=GRID, zeroline=False, linecolor=GRID)
    return fig


def show(fig: go.Figure, data: pd.DataFrame, key: str) -> None:
    st.plotly_chart(fig, width="stretch", config={"displayModeBar": False}, key=key)
    with st.expander("Data", expanded=False):
        st.dataframe(data, hide_index=True, width="stretch")


def empty(title: str) -> None:
    st.markdown(f"**{title}**")
    st.caption("No data for these filters yet.")


def stacked_bar(df: pd.DataFrame, x: str, series: str, y: str, colors: dict, title: str, key: str) -> None:
    if df.empty:
        return empty(title)
    fig = go.Figure()
    for name in [s for s in colors if s in set(df[series])]:
        part = df[df[series] == name]
        fig.add_bar(x=part[x], y=part[y], name=name, marker={"color": colors[name], "cornerradius": 4,
                                                              "line": {"color": SURFACE, "width": 2}},
                    hovertemplate=f"{name}<br>%{{x}}: %{{y}}<extra></extra>")
    fig.update_layout(barmode="stack")
    fig.update_xaxes(type="category")  # days/weeks are labels, not timestamps
    show(_layout(fig, title, legend=True), df, key)


def hbar(df: pd.DataFrame, label: str, value: str, title: str, key: str, fmt: str = "%{x}",
         color: str = DESK) -> None:
    if df.empty:
        return empty(title)
    d = df.sort_values(value)
    fig = go.Figure(go.Bar(x=d[value], y=d[label], orientation="h", marker={"color": color, "cornerradius": 4},
                           text=d[value], texttemplate=fmt.replace("%{x}", "%{text}"), textposition="outside",
                           cliponaxis=False, hovertemplate=f"%{{y}}: {fmt}<extra></extra>"))
    fig.update_xaxes(showgrid=True, gridcolor=GRID)
    if fmt.endswith("%}"):  # a rate (e.g. "%{x:.0%}"): fixed 0–100% scale, so a 0% bar reads as empty rather than as a mid-axis tick
        fig.update_xaxes(range=[0, 1.12], tickformat=".0%")
    fig.update_yaxes(showgrid=False)
    show(_layout(fig, title, height=max(220, 34 * len(d) + 70)), df, key)
