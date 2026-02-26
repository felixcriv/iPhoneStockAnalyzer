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
    --sort-by FIELD     Sort by: name, ticker, exchange, price, change, mktcap (default: name)
    --top N             Show only the top N suppliers by market cap
"""

import argparse
import json
import sys
from pathlib import Path

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
    p.add_argument("--sort-by", choices=list(SORT_KEYS), default="name",
                   metavar="FIELD", help="Sort field: " + ", ".join(SORT_KEYS))
    p.add_argument("--top", type=int, metavar="N",
                   help="Show top N suppliers by market cap")
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

    suppliers = load_suppliers(args.data)

    # Apply pre-fetch filters (country, tier, component) to avoid unnecessary API calls
    suppliers = apply_filters(suppliers, args)

    if not suppliers:
        print("No publicly traded suppliers match the given filters.")
        sys.exit(0)

    suppliers = fetch_prices(suppliers)

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

    print()
    print(tabulate(rows, headers=headers, tablefmt="rounded_outline"))
    print(f"\n{len(rows)} suppliers shown  |  Data: Yahoo Finance via yfinance")


if __name__ == "__main__":
    main()
