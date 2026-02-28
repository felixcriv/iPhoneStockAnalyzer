#!/usr/bin/env python3
"""Streamlit dashboard for the iPhone Supplier Stock Analyzer.

Run with:
    streamlit run app.py
"""

import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from plotly.subplots import make_subplots

from track_suppliers import (
    MARKET_CLOSE_HOUR,
    MARKET_CLOSE_MINUTE,
    _ET,
    convert_to_usd,
    fetch_fx_rates,
    fetch_prices,
    fmt_change,
    fmt_mktcap,
    fmt_price,
    load_suppliers,
)

# ---------------------------------------------------------------------------
# Page config
# ---------------------------------------------------------------------------

st.set_page_config(
    page_title="iPhone Supplier Tracker",
    page_icon="📱",
    layout="wide",
)

DATA_PATH = Path(__file__).parent / "data" / "iphone_suppliers.json"
CSV_PATH  = Path(__file__).parent / "data" / "stock_prices.csv"


# ---------------------------------------------------------------------------
# Market status helper
# ---------------------------------------------------------------------------

def market_status() -> tuple[str, float]:
    """Return (status_label, seconds_until_close).

    status_label: 'open' | 'pre-market' | 'closed' | 'weekend'
    seconds_until_close: positive when market is still open, -1 otherwise.
    """
    now = datetime.now(_ET)
    if now.weekday() >= 5:
        return "weekend", -1.0
    close    = now.replace(hour=MARKET_CLOSE_HOUR, minute=MARKET_CLOSE_MINUTE, second=0, microsecond=0)
    open_time = now.replace(hour=9, minute=30, second=0, microsecond=0)
    if now >= close:
        return "closed", -1.0
    if now < open_time:
        return "pre-market", (close - now).total_seconds()
    return "open", (close - now).total_seconds()


# ---------------------------------------------------------------------------
# Cached data loaders
# ---------------------------------------------------------------------------

@st.cache_data(show_spinner=False)
def _load_all_suppliers(data_path: str) -> list[dict]:
    return load_suppliers(data_path)


@st.cache_data(ttl=300, show_spinner=False)
def _fetch_live(data_path: str, convert: bool) -> list[dict]:
    """Fetch live prices for all suppliers; result cached for 5 minutes."""
    suppliers = load_suppliers(data_path)
    enriched  = fetch_prices(suppliers)
    if convert:
        currencies = {s["currency"] for s in enriched if s.get("currency")}
        fx_rates   = fetch_fx_rates(currencies)
        enriched   = convert_to_usd(enriched, fx_rates)
    return enriched


# ---------------------------------------------------------------------------
# UI helpers
# ---------------------------------------------------------------------------

def _filter(suppliers: list[dict], tier: str, countries: list[str], components: list[str]) -> list[dict]:
    result = suppliers
    if tier == "Direct only":
        result = [s for s in result if s["tier"] == "direct"]
    elif tier == "Indirect only":
        result = [s for s in result if s["tier"] == "indirect"]
    if countries:
        cl = {c.lower() for c in countries}
        result = [s for s in result if s["country"].lower() in cl]
    if components:
        def matches(s):
            joined = " ".join(s["components"]).lower()
            return any(c.lower() in joined for c in components)
        result = [s for s in result if matches(s)]
    return result


def _build_geo_chart(suppliers: list[dict]) -> go.Figure:
    """Choropleth + stacked bar — returns Figure without opening a browser."""
    agg: dict[str, dict] = defaultdict(lambda: {"direct": [], "indirect": []})
    for s in suppliers:
        country = s.get("country") or "Unknown"
        label   = s.get("short_name") or s.get("company_name") or s.get("ticker")
        agg[country][s.get("tier", "indirect")].append(label)

    sorted_ctry     = sorted(agg.items(), key=lambda x: len(x[1]["direct"]) + len(x[1]["indirect"]), reverse=True)
    country_names   = [c for c, _ in sorted_ctry]
    direct_counts   = [len(v["direct"])   for _, v in sorted_ctry]
    indirect_counts = [len(v["indirect"]) for _, v in sorted_ctry]
    total_counts    = [d + i for d, i in zip(direct_counts, indirect_counts)]

    def company_list(v):
        parts = []
        if v["direct"]:
            parts.append("<b>Direct:</b> " + ", ".join(v["direct"]))
        if v["indirect"]:
            parts.append("<b>Indirect:</b> " + ", ".join(v["indirect"]))
        return "<br>".join(parts)

    hover_texts = [
        f"<b>{c}</b><br>Total: {t}<br>{company_list(v)}"
        for (c, v), t in zip(sorted_ctry, total_counts)
    ]

    fig = make_subplots(
        rows=2, cols=1,
        row_heights=[0.55, 0.45],
        specs=[[{"type": "choropleth"}], [{"type": "xy"}]],
        subplot_titles=["Supplier Headquarters by Country", "Supplier Count per Country (Direct vs Indirect)"],
        vertical_spacing=0.06,
    )
    fig.add_trace(go.Choropleth(
        locations=country_names, locationmode="country names",
        z=total_counts, text=hover_texts, hoverinfo="text",
        colorscale="Blues", colorbar=dict(title="Suppliers", len=0.5, y=0.75),
        marker_line_color="white", marker_line_width=0.5,
    ), row=1, col=1)
    fig.add_trace(go.Bar(
        name="Direct", x=country_names, y=direct_counts,
        marker_color="#1D4ED8",
        hovertemplate="<b>%{x}</b><br>Direct: %{y}<extra></extra>",
    ), row=2, col=1)
    fig.add_trace(go.Bar(
        name="Indirect", x=country_names, y=indirect_counts,
        marker_color="#93C5FD",
        hovertemplate="<b>%{x}</b><br>Indirect: %{y}<extra></extra>",
    ), row=2, col=1)
    fig.update_layout(
        barmode="stack", height=820, template="plotly_white",
        legend=dict(orientation="h", yanchor="top", y=0.42, xanchor="right", x=1),
        margin=dict(l=20, r=20, t=50, b=20),
    )
    fig.update_yaxes(title_text="Number of suppliers", row=2, col=1)
    return fig


def _color_change(val: str) -> str:
    try:
        num = float(val.replace("%", "").replace("+", ""))
        if num > 0:
            return "color: #16a34a; font-weight:600"
        if num < 0:
            return "color: #dc2626; font-weight:600"
    except (ValueError, AttributeError):
        pass
    return ""


# ---------------------------------------------------------------------------
# At-close scheduling (session-state based countdown)
# ---------------------------------------------------------------------------

# Check if a scheduled fetch is pending before rendering anything else
if st.session_state.get("fetch_at_close"):
    status_now, secs = market_status()
    if secs <= 0:
        # Market just closed — clear cache and cancel the schedule
        st.session_state.fetch_at_close = False
        st.cache_data.clear()
    else:
        h, rem = divmod(int(secs), 3600)
        m, s   = divmod(rem, 60)
        st.info(f"⏳ Scheduled to fetch at market close — **{h:02d}:{m:02d}:{s:02d}** remaining")
        if st.button("Cancel scheduled fetch"):
            st.session_state.fetch_at_close = False
            st.rerun()
        time.sleep(1)
        st.rerun()


# ---------------------------------------------------------------------------
# Sidebar
# ---------------------------------------------------------------------------

with st.sidebar:
    st.header("Filters")

    tier_filter = st.radio("Supplier tier", ["All", "Direct only", "Indirect only"])

    all_raw       = _load_all_suppliers(str(DATA_PATH))
    all_countries = sorted({s["country"] for s in all_raw if s["country"]})
    all_components = sorted({
        comp.split(" (")[0]
        for s in all_raw
        for comp in s.get("components", [])
    })

    country_filter   = st.multiselect("Country", all_countries)
    component_filter = st.multiselect("Component", all_components)

    st.divider()

    sort_by    = st.selectbox("Sort by", ["mktcap", "price", "change", "name", "ticker", "exchange"])
    top_n      = st.number_input("Top N  (0 = all)", min_value=0, value=0, step=5)
    convert_usd = st.toggle("Convert to USD", value=True)

    st.divider()

    # Market status badge
    st.subheader("Market")
    status, secs_until_close = market_status()
    badge = {"open": "🟢 OPEN", "pre-market": "🟡 PRE-MARKET", "closed": "🔴 CLOSED", "weekend": "⚫ WEEKEND"}
    st.markdown(f"**{badge.get(status, status)}**")
    if secs_until_close > 0:
        h, rem = divmod(int(secs_until_close), 3600)
        m, s   = divmod(rem, 60)
        st.caption(f"Closes in {h:02d}:{m:02d}:{s:02d} ET")

    st.divider()

    if st.button("🔄 Fetch Live Prices", use_container_width=True):
        st.cache_data.clear()
        st.rerun()

    if st.button(
        "⏰ Fetch at Market Close",
        use_container_width=True,
        disabled=(status not in {"open", "pre-market"}),
        help="Waits until 4:00 PM ET, then auto-fetches prices",
    ):
        st.session_state.fetch_at_close = True
        st.rerun()


# ---------------------------------------------------------------------------
# Main content
# ---------------------------------------------------------------------------

st.title("📱 iPhone Supplier Stock Tracker")

# Load prices (cached) then filter in-memory
with st.spinner("Loading supplier prices…"):
    all_priced = _fetch_live(str(DATA_PATH), convert_usd)

suppliers = _filter(all_priced, tier_filter, country_filter, component_filter)

if not suppliers:
    st.warning("No suppliers match the current filters.")
    st.stop()

# Sort
SORT_MAP = {
    "name":     lambda r: (r["company_name"] or "").lower(),
    "ticker":   lambda r: (r["ticker"] or "").lower(),
    "exchange": lambda r: (r["exchange"] or "").lower(),
    "price":    lambda r: r["price"] or -1,
    "change":   lambda r: r["change_pct"] or -9999,
    "mktcap":   lambda r: r["market_cap"] or -1,
}
suppliers.sort(key=SORT_MAP[sort_by], reverse=(sort_by in {"price", "change", "mktcap"}))

if top_n > 0:
    by_cap      = sorted(suppliers, key=lambda r: r["market_cap"] or -1, reverse=True)
    top_tickers = {s["ticker"] for s in by_cap[:top_n]}
    suppliers   = [s for s in suppliers if s["ticker"] in top_tickers]

# ---------------------------------------------------------------------------
# Metrics row
# ---------------------------------------------------------------------------

priced      = [s for s in suppliers if s["price"] is not None]
total_mktcap = sum(s["market_cap"] for s in suppliers if s["market_cap"])
changes     = [s["change_pct"] for s in priced if s["change_pct"] is not None]
avg_change  = sum(changes) / len(changes) if changes else 0.0
n_countries = len({s["country"] for s in suppliers})

c1, c2, c3, c4 = st.columns(4)
c1.metric("Suppliers",       len(suppliers))
c2.metric("Countries",       n_countries)
c3.metric("Total Market Cap", fmt_mktcap(total_mktcap))
c4.metric("Avg Day Change",  f"{avg_change:+.2f}%")

st.divider()

# ---------------------------------------------------------------------------
# Supplier table
# ---------------------------------------------------------------------------

st.subheader("Supplier Stock Prices")

currency_note = "USD" if convert_usd else "native currency"

rows = []
for s in suppliers:
    rows.append({
        "Company":    s["short_name"] or s["company_name"],
        "Ticker":     s["ticker"],
        "Exchange":   s["exchange"],
        "Country":    s["country"],
        "Tier":       s["tier"],
        "Price":      fmt_price(s["price"], s["currency"]),
        "Change":     fmt_change(s["change_pct"]),
        "Mkt Cap":    fmt_mktcap(s["market_cap"]),
        "Confidence": s["confidence"],
    })

df     = pd.DataFrame(rows)
styled = df.style.map(_color_change, subset=["Change"])
st.dataframe(styled, use_container_width=True, hide_index=True)
st.caption(
    f"{len(suppliers)} suppliers shown  ·  prices in {currency_note}  ·  "
    f"source: Yahoo Finance via yfinance  ·  cache TTL: 5 min"
)

st.divider()

# ---------------------------------------------------------------------------
# Geographic distribution chart
# ---------------------------------------------------------------------------

st.subheader("Geographic Distribution")
st.plotly_chart(_build_geo_chart(suppliers), use_container_width=True)

# ---------------------------------------------------------------------------
# Historical snapshots
# ---------------------------------------------------------------------------

if CSV_PATH.exists():
    st.divider()
    st.subheader("Historical Price Snapshots")

    hist = pd.read_csv(CSV_PATH, parse_dates=["fetched_at"])

    if not hist.empty:
        visible_tickers = [s["ticker"] for s in suppliers]
        hist_view       = hist[hist["ticker"].isin(visible_tickers)]

        if hist_view.empty:
            st.info("No historical records yet for the current filter selection.")
        else:
            available = sorted(hist_view["ticker"].unique())
            selected  = st.multiselect(
                "Tickers to plot",
                options=available,
                default=available[:min(6, len(available))],
            )
            if selected:
                fig2 = go.Figure()
                for ticker in selected:
                    td = hist_view[hist_view["ticker"] == ticker].sort_values("fetched_at")
                    short = next(
                        (s["short_name"] or s["company_name"] for s in suppliers if s["ticker"] == ticker),
                        ticker,
                    )
                    fig2.add_trace(go.Scatter(
                        x=td["fetched_at"], y=td["price"],
                        mode="lines+markers", name=f"{short} ({ticker})",
                    ))
                fig2.update_layout(
                    xaxis_title="Date (UTC)",
                    yaxis_title=f"Price ({'USD' if convert_usd else 'native'})",
                    template="plotly_white",
                    height=420,
                    legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
                    margin=dict(l=20, r=20, t=20, b=20),
                )
                st.plotly_chart(fig2, use_container_width=True)
