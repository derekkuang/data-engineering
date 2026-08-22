"""Kalshi Opportunity Radar — the serving layer over fct_kalshi_opportunity.

Reads a daily snapshot of the universe-opportunity mart (per-series spread-capture,
near-money spread, volume, maker-fee flag) and renders the maker-opportunity landscape:
where wide retail spreads meet real volume, and which books are maker-fee-taxed.

Offline-first: reads dashboard/data/opportunity_snapshot.parquet (exported from Athena by
publish_snapshot.py), so it runs and deploys with no AWS creds. `spread_capture` is the GROSS
half-spread $/day upper bound — a relative ranking of where to look, NOT realized P&L.

Run:  uv run --group dashboard streamlit run dashboard/app.py
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

# --- validated palette (dataviz skill; light surface) --------------------------------
FREE = "#2a78d6"      # maker-free (categorical slot 1, blue)
TAXED = "#eb6834"     # maker-fee-taxed (slot 8, orange) — CVD ΔE 96.7 vs blue
CATEGORICAL = ["#2a78d6", "#1baf7a", "#eda100", "#008300",
               "#4a3aa7", "#e34948", "#e87ba4", "#eb6834"]
INK = "#0b0b0b"
INK_2 = "#52514e"
MUTED = "#898781"
GRID = "#e1e0d9"
BAND = "rgba(42,120,214,0.07)"  # retail-spread band wash (blue, faint)

RETAIL_LO, RETAIL_HI = 2.0, 15.0   # the retail spread band the soccer MM edge lives in (cents)
SWEET_VOL = 5000.0                 # a "real flow" floor for the sweet-spot filter (contracts/24h)
DATA = Path(__file__).parent / "data" / "opportunity_snapshot.parquet"


@st.cache_data
def load() -> pd.DataFrame:
    df = pd.read_parquet(DATA)
    return df[df["snapshot_day"] == df["snapshot_day"].max()].copy()


def _layout(fig: go.Figure, *, height: int = 420) -> go.Figure:
    """Recessive chrome, ink-token text, transparent surface (adapts to the Streamlit theme)."""
    fig.update_layout(
        height=height, paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
        font=dict(family="system-ui, -apple-system, Segoe UI, sans-serif", color=INK_2, size=13),
        margin=dict(l=10, r=10, t=30, b=10),
        legend=dict(orientation="h", yanchor="bottom", y=1.02, x=0),
    )
    fig.update_xaxes(gridcolor=GRID, zeroline=False, linecolor=GRID, title_font_color=INK_2)
    fig.update_yaxes(gridcolor=GRID, zeroline=False, linecolor=GRID, title_font_color=INK_2)
    return fig


def scatter(df: pd.DataFrame) -> go.Figure:
    """THE insight chart: near-money spread (y) vs 24h volume (x, log), maker-fee colored.
    Reads the whole structure at a glance — crowded/efficient (tight + high-volume, bottom-right),
    empty-book traps (~100c wide, top), and the sweet spot (retail band x real volume)."""
    d = df[(df["n_near_money_markets"] > 0) & df["median_near_money_spread_c"].notna()].copy()
    d = d[d["volume_24h"] > 0]
    fig = go.Figure()
    # shaded retail-spread band (the makeable zone)
    fig.add_hrect(y0=RETAIL_LO, y1=RETAIL_HI, fillcolor=BAND, line_width=0, layer="below")
    for taxed, color, name in [(False, FREE, "maker-free"), (True, TAXED, "maker-fee")]:
        s = d[d["has_maker_fee"] == taxed]
        fig.add_trace(go.Scatter(
            x=s["volume_24h"], y=s["median_near_money_spread_c"], mode="markers", name=name,
            marker=dict(
                color=color, size=s["spread_capture"].clip(lower=1) ** 0.28,
                sizemode="area", sizeref=0.9, sizemin=5,
                line=dict(width=1, color="rgba(255,255,255,0.7)"),  # 2px surface ring on overlap
            ),
            customdata=s[["series_ticker", "category", "spread_capture", "n_markets"]],
            hovertemplate=("<b>%{customdata[0]}</b> (%{customdata[1]})<br>"
                           "near-money spread: %{y:.1f}c<br>24h volume: %{x:,.0f}<br>"
                           "gross capture: $%{customdata[2]:,.0f}/day<br>"
                           "%{customdata[3]} markets<extra></extra>"),
        ))
    fig.update_xaxes(type="log", title="24h volume (contracts, log)")
    fig.update_yaxes(title="median near-money spread (c)", range=[0, 102])
    fig.add_annotation(x=0, y=RETAIL_HI, xref="paper", yref="y", text="retail band 2–15c",
                       showarrow=False, xanchor="left", yanchor="bottom",
                       font=dict(color=MUTED, size=11))
    return _layout(fig, height=460)


def category_bar(df: pd.DataFrame) -> go.Figure:
    """Gross capture by category — direct value labels satisfy the relief rule for the ramp."""
    g = (df.groupby("category")["spread_capture"].sum().sort_values(ascending=True)).tail(10)
    colors = [CATEGORICAL[i % len(CATEGORICAL)] for i in range(len(g))]
    fig = go.Figure(go.Bar(
        x=g.values, y=g.index, orientation="h", marker_color=colors,
        text=[f"${v:,.0f}" for v in g.values], textposition="outside",
        textfont=dict(color=INK_2, size=11),
        hovertemplate="<b>%{y}</b><br>gross capture: $%{x:,.0f}/day<extra></extra>",
    ))
    fig.update_xaxes(title="gross spread-capture ($/day)")
    return _layout(fig, height=360)


def main() -> None:
    st.set_page_config(page_title="Kalshi Opportunity Radar", layout="wide", page_icon="📡")
    df = load()
    day = str(df["snapshot_day"].max())

    st.title("Kalshi Opportunity Radar")
    st.caption(
        f"Where a market-maker could earn on Kalshi — per-series spread-capture landscape, "
        f"snapshot **{day}**. `spread_capture` is the GROSS half-spread $/day **upper bound** "
        f"(assumes you win every fill; not P&L, not toxicity-adjusted). Source: dbt "
        f"`fct_kalshi_opportunity` over the daily universe snapshot."
    )

    # --- KPI stat tiles ---
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Open markets", f"{int(df['n_markets'].sum()):,}")
    c2.metric("Series", f"{df['series_ticker'].nunique():,}")
    c3.metric("Maker-fee series", f"{int(df['has_maker_fee'].sum()):,}",
              help="Series on Kalshi's Feb-2026 maker-fee list — a maker fee eats a thin edge.")
    c4.metric("Gross capture $/day", f"${df['spread_capture'].sum():,.0f}",
              help="Upper bound across all series; a relative ranking, not realized P&L.")

    # --- filters, one row above the charts ---
    st.divider()
    f1, f2, f3 = st.columns([2, 1, 1])
    cats = sorted(df["category"].dropna().unique())
    picked = f1.multiselect("Category", cats, default=cats)
    no_fee = f2.toggle("Exclude maker-fee series", value=False)
    sweet = f3.toggle("Sweet spot only", value=False,
                      help=f"Near-money spread {RETAIL_LO:.0f}–{RETAIL_HI:.0f}c AND "
                           f"≥{SWEET_VOL:,.0f} contracts/24h — real spread + real flow.")

    view = df[df["category"].isin(picked)]
    if no_fee:
        view = view[~view["has_maker_fee"]]
    if sweet:
        view = view[
            view["median_near_money_spread_c"].between(RETAIL_LO, RETAIL_HI)
            & (view["volume_24h"] >= SWEET_VOL)
        ]

    if view.empty:
        st.warning("No series match the current filters.")
        return

    st.plotly_chart(scatter(view), width='stretch')

    left, right = st.columns([3, 2])
    with left:
        st.subheader("Top series by gross capture")
        top = view.nlargest(15, "spread_capture")[[
            "series_ticker", "category", "n_markets", "n_near_money_markets",
            "median_near_money_spread_c", "volume_24h", "spread_capture", "has_maker_fee",
        ]].rename(columns={
            "series_ticker": "series", "n_markets": "mkts", "n_near_money_markets": "near",
            "median_near_money_spread_c": "near_spr_c", "volume_24h": "vol_24h",
            "spread_capture": "capture_$", "has_maker_fee": "maker_fee",
        })
        fmt = {"near_spr_c": "{:.1f}", "vol_24h": "{:,.0f}", "capture_$": "${:,.0f}"}
        st.dataframe(
            top.style.format(fmt), hide_index=True, width='stretch',
        )
    with right:
        st.subheader("Capture by category")
        st.plotly_chart(category_bar(view), width='stretch')

    st.caption(
        "Read: the shaded 2–15c band on the scatter is the makeable retail zone; points at ~100c "
        "are empty/one-sided books (wide ≠ opportunity), points near 1c are crowded/efficient. "
        "Orange = maker-fee-taxed (avoid for a thin edge). The real target is blue, in-band, far "
        "right (real volume). Toxicity — whether that flow is benign or picks you off — is the "
        "next layer, not shown here."
    )


if __name__ == "__main__":
    main()
