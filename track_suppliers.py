#!/usr/bin/env python3
"""
track_suppliers.py — Fetch and display live stock prices for iPhone suppliers.

Usage:
    python track_suppliers.py [options]

Options:
    --data PATH         Path to supplier JSON file (default: data/iphone_suppliers.json)
    --component ID      Filter by component_id (can be repeated)
    --country COUNTRY   Filter by country (can be repeated)
    --direct-only       Show only direct suppliers
    --indirect-only     Show only indirect suppliers
    --sort-by FIELD     Sort by: name, ticker, exchange, price, change, mktcap (default: price)
    --top N             Show only the top N suppliers by market cap
    --output PATH       Append results to a CSV file (creates it if missing)
    --no-usd            Keep prices in each stock's native currency instead of converting to USD
    --graph             Open an interactive country-distribution chart in the browser
    --at-close          Wait until US market close (4:00 PM ET) before fetching data
"""

import argparse
import csv
import json
import sys
import time
import webbrowser
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

try:
    import yfinance as yf
except ImportError:
    sys.exit("Missing dependency: run  pip install -r requirements.txt")

try:
    from tabulate import tabulate
except ImportError:
    sys.exit("Missing dependency: run  pip install -r requirements.txt")


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def load_suppliers(data_path: str) -> list[dict]:
    """Read the supplier JSON and return a flat, deduplicated list of publicly
    traded suppliers, each augmented with metadata about which components and
    roles they serve."""
    path = Path(data_path)
    if not path.exists():
        sys.exit(f"Data file not found: {path}")

    with path.open() as f:
        data = json.load(f)

    seen: dict[str, dict] = {}  # ticker -> supplier record

    for component in data.get("components", []):
        comp_name = component["component_name"]
        comp_id = component["component_id"]

        for tier, suppliers in (
            ("direct", component.get("direct_suppliers", [])),
            ("indirect", component.get("indirect_suppliers", [])),
        ):
            for s in suppliers:
                if not s.get("publicly_traded") or not s.get("ticker"):
                    continue

                ticker = s["ticker"]

                if ticker not in seen:
                    seen[ticker] = {
                        "ticker": ticker,
                        "exchange": s.get("exchange", ""),
                        "company_name": s.get("company_name", ""),
                        "short_name": s.get("short_name", ""),
                        "country": s.get("country", ""),
                        "tier": tier,
                        "components": [],
                        "confidence": s.get("relationship_confidence", ""),
                    }

                entry = seen[ticker]
                label = f"{comp_name} ({tier})"
                if label not in entry["components"]:
                    entry["components"].append(label)

                # Upgrade tier to "direct" if seen as both
                if tier == "direct":
                    entry["tier"] = "direct"

    return list(seen.values())


# ---------------------------------------------------------------------------
# Stock data fetching
# ---------------------------------------------------------------------------

def fetch_prices(suppliers: list[dict]) -> list[dict]:
    """Use yfinance to bulk-fetch quote data and attach it to each supplier."""
    tickers = [s["ticker"] for s in suppliers]

    print(f"Fetching quotes for {len(tickers)} tickers …", flush=True)

    # Bulk download is faster than individual calls
    raw = yf.Tickers(" ".join(tickers))

    enriched = []
    for s in suppliers:
        ticker = s["ticker"]
        try:
            info = raw.tickers[ticker].fast_info
            price = getattr(info, "last_price", None)
            prev_close = getattr(info, "previous_close", None)
            mkt_cap = getattr(info, "market_cap", None)
            currency = getattr(info, "currency", "")

            change_pct = None
            if price is not None and prev_close and prev_close != 0:
                change_pct = (price - prev_close) / prev_close * 100
        except Exception:
            price = prev_close = mkt_cap = currency = change_pct = None

        enriched.append({
            **s,
            "price": price,
            "prev_close": prev_close,
            "change_pct": change_pct,
            "market_cap": mkt_cap,
            "currency": currency or "",
        })

    return enriched


def fetch_fx_rates(currencies: set[str]) -> dict[str, float]:
    """Fetch exchange rates to USD for each non-USD currency via yfinance.
    Returns a dict of {currency_code: rate_to_usd}."""
    rates: dict[str, float] = {"USD": 1.0}
    pairs = {c: f"{c}USD=X" for c in currencies if c and c != "USD"}
    if not pairs:
        return rates

    print(f"Fetching FX rates for: {', '.join(sorted(pairs))} …", flush=True)
    raw = yf.Tickers(" ".join(pairs.values()))

    for currency, pair in pairs.items():
        try:
            rate = raw.tickers[pair].fast_info.last_price
            if rate:
                rates[currency] = rate
        except Exception:
            pass  # leave missing currencies unconverted

    return rates


def convert_to_usd(suppliers: list[dict], fx_rates: dict[str, float]) -> list[dict]:
    """Return a copy of suppliers with price, prev_close, and market_cap
    converted to USD using the provided FX rates."""
    converted = []
    for s in suppliers:
        rate = fx_rates.get(s.get("currency", "USD"), None)
        if rate is None or s["currency"] == "USD":
            converted.append({**s, "currency": "USD"})
            continue
        converted.append({
            **s,
            "price":      s["price"] * rate if s["price"] is not None else None,
            "prev_close": s["prev_close"] * rate if s["prev_close"] is not None else None,
            "market_cap": s["market_cap"] * rate if s["market_cap"] is not None else None,
            "currency":   "USD",
        })
    return converted


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def fmt_price(price, currency: str) -> str:
    if price is None:
        return "N/A"
    return f"{currency} {price:,.2f}" if currency else f"{price:,.2f}"


def fmt_change(change_pct) -> str:
    if change_pct is None:
        return "N/A"
    sign = "+" if change_pct >= 0 else ""
    return f"{sign}{change_pct:.2f}%"


def fmt_mktcap(mkt_cap) -> str:
    if mkt_cap is None:
        return "N/A"
    if mkt_cap >= 1e12:
        return f"${mkt_cap / 1e12:.2f}T"
    if mkt_cap >= 1e9:
        return f"${mkt_cap / 1e9:.2f}B"
    if mkt_cap >= 1e6:
        return f"${mkt_cap / 1e6:.2f}M"
    return f"${mkt_cap:,.0f}"


_GRAPH_DEFAULT_PATH = Path(__file__).parent / "docs" / "index.html"


def build_country_graph(suppliers: list[dict], output_path: Path | None = None) -> Path:
    """Build an interactive Plotly chart (choropleth + stacked bar) grouped by
    supplier country, save to *output_path* (default: output/country_map.html),
    and open it in the default browser."""
    if output_path is None:
        output_path = _GRAPH_DEFAULT_PATH
    try:
        import plotly.graph_objects as go
        from plotly.subplots import make_subplots
    except ImportError:
        sys.exit("Missing dependency: run  pip install -r requirements.txt")

    # Aggregate per country
    agg: dict[str, dict] = defaultdict(lambda: {"direct": [], "indirect": []})
    for s in suppliers:
        country = s.get("country") or "Unknown"
        tier = s.get("tier", "indirect")
        label = s.get("short_name") or s.get("company_name") or s.get("ticker")
        agg[country][tier].append(label)

    # Sort countries by total count (descending) for the bar chart
    sorted_ctry = sorted(agg.items(), key=lambda x: len(x[1]["direct"]) + len(x[1]["indirect"]), reverse=True)

    country_names  = [c for c, _ in sorted_ctry]
    direct_counts  = [len(v["direct"])   for _, v in sorted_ctry]
    indirect_counts= [len(v["indirect"]) for _, v in sorted_ctry]
    total_counts   = [d + i for d, i in zip(direct_counts, indirect_counts)]

    def company_list(v):
        lines = []
        if v["direct"]:
            lines.append("<b>Direct:</b> " + ", ".join(v["direct"]))
        if v["indirect"]:
            lines.append("<b>Indirect:</b> " + ", ".join(v["indirect"]))
        return "<br>".join(lines)

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

    # --- Choropleth world map ---
    fig.add_trace(
        go.Choropleth(
            locations=country_names,
            locationmode="country names",
            z=total_counts,
            text=hover_texts,
            hoverinfo="text",
            colorscale="Blues",
            colorbar=dict(title="Suppliers", len=0.5, y=0.75),
            showscale=True,
            marker_line_color="white",
            marker_line_width=0.5,
        ),
        row=1, col=1,
    )

    # --- Stacked bar chart ---
    fig.add_trace(
        go.Bar(
            name="Direct",
            x=country_names,
            y=direct_counts,
            marker_color="#1D4ED8",
            hovertemplate="<b>%{x}</b><br>Direct suppliers: %{y}<extra></extra>",
        ),
        row=2, col=1,
    )
    fig.add_trace(
        go.Bar(
            name="Indirect",
            x=country_names,
            y=indirect_counts,
            marker_color="#93C5FD",
            hovertemplate="<b>%{x}</b><br>Indirect suppliers: %{y}<extra></extra>",
        ),
        row=2, col=1,
    )

    fig.update_layout(
        title_text="iPhone Supplier Countries",
        title_font_size=22,
        barmode="stack",
        height=950,
        template="plotly_white",
        legend=dict(orientation="h", yanchor="top", y=0.42, xanchor="right", x=1),
        margin=dict(l=20, r=20, t=60, b=20),
    )
    fig.update_yaxes(title_text="Number of suppliers", row=2, col=1)

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fig.write_html(str(output_path))
    print(f"Graph saved → {output_path}")
    webbrowser.open(output_path.resolve().as_uri())
    return output_path


MARKET_CLOSE_HOUR = 16   # 4:00 PM ET
MARKET_CLOSE_MINUTE = 0
_ET = ZoneInfo("America/New_York")


def wait_for_market_close() -> None:
    """Block until 4:00 PM ET today, printing a live countdown.

    If the market has already closed (or it is a weekend), prints a notice
    and returns immediately so the script continues without delay.
    """
    now = datetime.now(_ET)
    close = now.replace(hour=MARKET_CLOSE_HOUR, minute=MARKET_CLOSE_MINUTE, second=0, microsecond=0)

    if now.weekday() >= 5:  # Saturday=5, Sunday=6
        print(f"Today is a weekend — market is closed. Running now ({now:%Y-%m-%d %H:%M %Z}).")
        return

    if now >= close:
        print(f"Market already closed at {close:%H:%M %Z}. Running now ({now:%H:%M %Z}).")
        return

    wait_seconds = (close - now).total_seconds()
    print(f"Waiting for market close at {close:%H:%M %Z} …  (≈{int(wait_seconds // 60)} min remaining)")

    while True:
        now = datetime.now(_ET)
        remaining = (close - now).total_seconds()
        if remaining <= 0:
            break
        h, rem = divmod(int(remaining), 3600)
        m, s = divmod(rem, 60)
        countdown = f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"
        print(f"\r  Time until close: {countdown}   ", end="", flush=True)
        time.sleep(1)

    print(f"\rMarket closed. Starting analysis …                    ")


SORT_KEYS = {
    "name":     lambda r: (r["company_name"] or "").lower(),
    "ticker":   lambda r: (r["ticker"] or "").lower(),
    "exchange": lambda r: (r["exchange"] or "").lower(),
    "price":    lambda r: r["price"] or -1,
    "change":   lambda r: r["change_pct"] or -9999,
    "mktcap":   lambda r: r["market_cap"] or -1,
}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Show live stock prices for iPhone suppliers.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--data", default="data/iphone_suppliers.json",
                   help="Path to supplier JSON file")
    p.add_argument("--component", metavar="ID", action="append", dest="components",
                   help="Filter by component_id (repeatable)")
    p.add_argument("--country", action="append", dest="countries",
                   help="Filter by country (repeatable)")
    p.add_argument("--direct-only", action="store_true",
                   help="Show only direct Apple suppliers")
    p.add_argument("--indirect-only", action="store_true",
                   help="Show only indirect suppliers")
    p.add_argument("--sort-by", choices=list(SORT_KEYS), default="price",
                   metavar="FIELD", help="Sort field: " + ", ".join(SORT_KEYS))
    p.add_argument("--top", type=int, metavar="N",
                   help="Show top N suppliers by market cap")
    p.add_argument("--output", metavar="PATH",
                   help="Append results to a CSV file (creates it if missing)")
    p.add_argument("--no-usd", action="store_true",
                   help="Keep prices in native currency instead of converting to USD")
    p.add_argument("--graph", action="store_true",
                   help="Open an interactive country-distribution chart in the browser")
    p.add_argument("--at-close", action="store_true",
                   help="Wait until US market close (4:00 PM ET) before fetching data")
    return p.parse_args()


def apply_filters(suppliers: list[dict], args: argparse.Namespace) -> list[dict]:
    result = suppliers

    if args.direct_only:
        result = [s for s in result if s["tier"] == "direct"]
    elif args.indirect_only:
        result = [s for s in result if s["tier"] == "indirect"]

    if args.countries:
        countries_lower = {c.lower() for c in args.countries}
        result = [s for s in result if s["country"].lower() in countries_lower]

    if args.components:
        comp_ids = set(args.components)
        # We stored component names in the list; filter by checking if any
        # component label starts with a matching component_id key. Since we
        # stored component names (not IDs) we reload the JSON here to check.
        # Simpler: just warn the user and do best-effort substring match.
        def matches_component(s):
            joined = " ".join(s["components"]).lower()
            return any(cid.lower().replace("_", " ") in joined for cid in comp_ids)
        result = [s for s in result if matches_component(s)]

    return result


def main():
    args = parse_args()

    if args.at_close:
        wait_for_market_close()

    suppliers = load_suppliers(args.data)

    # Apply pre-fetch filters (country, tier, component) to avoid unnecessary API calls
    suppliers = apply_filters(suppliers, args)

    if not suppliers:
        print("No publicly traded suppliers match the given filters.")
        sys.exit(0)

    suppliers = fetch_prices(suppliers)

    # USD conversion (default on; skip with --no-usd)
    if not args.no_usd:
        currencies = {s["currency"] for s in suppliers if s.get("currency")}
        fx_rates = fetch_fx_rates(currencies)
        suppliers = convert_to_usd(suppliers, fx_rates)

    # Sort
    sort_fn = SORT_KEYS.get(args.sort_by, SORT_KEYS["name"])
    suppliers.sort(key=sort_fn, reverse=(args.sort_by in {"price", "change", "mktcap"}))

    # Top N (by market cap after sorting)
    if args.top:
        by_mktcap = sorted(suppliers, key=lambda r: r["market_cap"] or -1, reverse=True)
        top_tickers = {s["ticker"] for s in by_mktcap[: args.top]}
        suppliers = [s for s in suppliers if s["ticker"] in top_tickers]

    # Build table
    headers = ["Company", "Ticker", "Exchange", "Country", "Tier",
               "Price", "Change", "Mkt Cap", "Confidence"]

    rows = []
    for s in suppliers:
        rows.append([
            s["short_name"] or s["company_name"],
            s["ticker"],
            s["exchange"],
            s["country"],
            s["tier"],
            fmt_price(s["price"], s["currency"]),
            fmt_change(s["change_pct"]),
            fmt_mktcap(s["market_cap"]),
            s["confidence"],
        ])

    currency_note = "prices in USD" if not args.no_usd else "prices in native currency"
    print()
    print(tabulate(rows, headers=headers, tablefmt="rounded_outline"))
    print(f"\n{len(rows)} suppliers shown  |  {currency_note}  |  Data: Yahoo Finance via yfinance")

    if args.output:
        append_to_csv(suppliers, args.output)

    if args.graph:
        build_country_graph(suppliers)


def append_to_csv(suppliers: list[dict], output_path: str) -> None:
    """Append the current snapshot to a CSV file, adding a header row if new."""
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    is_new = not path.exists() or path.stat().st_size == 0

    fetched_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    fieldnames = [
        "fetched_at", "ticker", "exchange", "company_name", "short_name",
        "country", "tier", "currency", "price", "prev_close", "change_pct",
        "market_cap", "confidence",
    ]

    with path.open("a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        if is_new:
            writer.writeheader()
        for s in suppliers:
            writer.writerow({**s, "fetched_at": fetched_at})

    print(f"Results appended to {path}  ({len(suppliers)} rows, timestamp: {fetched_at})")


if __name__ == "__main__":
    main()
