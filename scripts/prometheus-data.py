#!/usr/bin/env python3
"""
prometheus-data.py — Live Data Fetcher for Prometheus Fundamental Analysis

Fetches macroeconomic, on-chain, and project-level fundamentals from
public APIs. Designed to be run from the Prometheus trading skill.

Usage:
    python3 prometheus-data.py --coin bitcoin
    python3 prometheus-data.py --coin ethereum --sector
    python3 prometheus-data.py --coin solana --json
    python3 prometheus-data.py --list-coins

Dependencies: python3 standard lib (urllib, json)
"""

import argparse
import functools
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

# ── Retry Decorator ────────────────────────────────────────────────────────────

def retry(max_attempts=3, delay=2):
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            for attempt in range(max_attempts):
                try:
                    return func(*args, **kwargs)
                except Exception as e:
                    if attempt == max_attempts - 1:
                        raise
                    print(f'  [retry] Attempt {attempt+1} failed: {e}. Retrying in {delay}s...', file=sys.stderr)
                    time.sleep(delay)
            return None
        return wrapper
    return decorator

# ── Cache Decorator ────────────────────────────────────────────────────────────

CACHE_DIR = os.path.expanduser('~/.cache/telos-agents')
os.makedirs(CACHE_DIR, exist_ok=True)

def cached(ttl_seconds=300):
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            cache_key = f'{func.__name__}_{hash(str(args) + str(sorted(kwargs.items())))}'
            cache_path = os.path.join(CACHE_DIR, f'{cache_key}.json')
            if os.path.exists(cache_path):
                age = time.time() - os.path.getmtime(cache_path)
                if age < ttl_seconds:
                    with open(cache_path) as f:
                        return json.load(f)
            result = func(*args, **kwargs)
            with open(cache_path, 'w') as f:
                json.dump(result, f)
            return result
        return wrapper
    return decorator

# ── Configuration ──────────────────────────────────────────────────────────────

COINGECKO_BASE = "https://api.coingecko.com/api/v3"
ALTERNATIVE_ME_BASE = "https://api.alternative.me/fng"
DEFILLAMA_BASE = "https://api.llama.fi"
TOKENUNLOCKS_BASE = "https://api.tokenunlocks.com/api/v1/token"

# Known coin ID mappings (CoinGecko IDs)
COIN_IDS = {
    "bitcoin": "bitcoin", "btc": "bitcoin",
    "ethereum": "ethereum", "eth": "ethereum",
    "solana": "solana", "sol": "solana",
    "cardano": "cardano", "ada": "cardano",
    "ripple": "ripple", "xrp": "ripple",
    "polkadot": "polkadot", "dot": "polkadot",
    "avalanche": "avalanche-2", "avax": "avalanche-2",
    "chainlink": "chainlink", "link": "chainlink",
    "polygon": "matic-network", "matic": "matic-network",
    "arbitrum": "arbitrum", "arb": "arbitrum",
    "optimism": "optimism", "op": "optimism",
    "sui": "sui", "aptos": "aptos", "apt": "aptos",
    "near": "near",
    "injective": "injective-protocol", "inj": "injective-protocol",
    "render": "render-token", "rndr": "render-token",
    "ai16z": "ai16z",
    "virtuals": "virtual-protocol", "virtual": "virtual-protocol",
}

# Known DefiLlama protocol slugs
PROTOCOL_SLUGS = {
    "lido": "lido", "uniswap": "uniswap", "aave": "aave",
    "makerdao": "makerdao", "maker": "makerdao",
    "eigenlayer": "eigenlayer", "ethena": "ethena",
    "pendle": "pendle", "jupiter": "jupiter",
    "raydium": "raydium", "aerodrome": "aerodrome-finance",
}

# ── Data Freshness Tracking ────────────────────────────────────────────────────

DATA_FRESHNESS = {}

def _record_freshness(source):
    DATA_FRESHNESS[source] = time.time()

def fresh_since(source):
    if source in DATA_FRESHNESS:
        return f"{(time.time() - DATA_FRESHNESS[source]) / 60:.0f}"
    return "never"

def _stale_warning(source, max_age_minutes=60):
    """Print warning if data is stale."""
    if source in DATA_FRESHNESS:
        age_min = (time.time() - DATA_FRESHNESS[source]) / 60
        if age_min > max_age_minutes:
            print(f"  [stale] WARNING: {source} data is {age_min:.0f}m old (max {max_age_minutes}m)", file=sys.stderr)

# ── Helpers ────────────────────────────────────────────────────────────────────

def _fetch(url, timeout=15):
    """Fetch JSON from a URL. Returns dict or None on failure."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Prometheus/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        return {"_error": f"HTTP {e.code}: {e.reason}"}
    except urllib.error.URLError as e:
        return {"_error": f"URL Error: {e.reason}"}
    except json.JSONDecodeError as e:
        return {"_error": f"JSON parse error: {e}"}
    except Exception as e:
        return {"_error": str(e)}


def _fmt(val, suffix=""):
    """Format a number with commas, optional suffix."""
    if val is None:
        return "N/A"
    try:
        v = float(val)
        if v >= 1_000_000_000:
            return f"${v / 1_000_000_000:,.2f}B{suffix}"
        if v >= 1_000_000:
            return f"${v / 1_000_000:,.2f}M{suffix}"
        if v >= 1_000:
            return f"${v:,.0f}{suffix}"
        return f"{v:,.4f}"
    except (ValueError, TypeError):
        return str(val)


def _pct(val):
    """Format a percentage."""
    if val is None:
        return "N/A"
    try:
        v = float(val)
        sign = "+" if v > 0 else ""
        return f"{sign}{v:.2f}%"
    except (ValueError, TypeError):
        return str(val)


# ── Data Fetchers ──────────────────────────────────────────────────────────────

@cached(ttl_seconds=180)
@retry(max_attempts=3, delay=2)
def fear_greed_index():
    """Fetch Fear & Greed Index from alternative.me."""
    try:
        data = _fetch(f"{ALTERNATIVE_ME_BASE}/?limit=7")
        if not data or "data" not in data:
            return None
        today = data["data"][0]
        week_ago = data["data"][-1] if len(data["data"]) > 1 else None
        result = {
            "value": today.get("value"),
            "classification": today.get("value_classification"),
            "timestamp": today.get("timestamp"),
            "trend": (
                f"{week_ago.get('value')} -> {today.get('value')} ({today.get('value_classification')})"
                if week_ago else None
            ),
            "_fetched_at": time.time(),
        }
        _record_freshness("fear_greed")
        return result
    except Exception as e:
        return {"_error": f"Fear & Greed fetch failed: {e}"}


@cached(ttl_seconds=180)
@retry(max_attempts=3, delay=2)
def global_data():
    """Fetch global market data from CoinGecko."""
    try:
        data = _fetch(f"{COINGECKO_BASE}/global")
        if not data or "data" not in data:
            return None
        d = data["data"]
        result = {
            "total_market_cap": d.get("total_market_cap", {}).get("usd"),
            "total_volume_24h": d.get("total_volume", {}).get("usd"),
            "btc_dominance": d.get("market_cap_percentage", {}).get("btc"),
            "eth_dominance": d.get("market_cap_percentage", {}).get("eth"),
            "active_cryptos": d.get("active_cryptocurrencies"),
            "market_cap_change_24h": d.get("market_cap_change_percentage_24h_usd"),
            "_fetched_at": time.time(),
        }
        _record_freshness("global_data")
        return result
    except Exception as e:
        return {"_error": f"Global data fetch failed: {e}"}


@cached(ttl_seconds=180)
@retry(max_attempts=3, delay=2)
def coin_data(coin_id):
    """Fetch detailed project data from CoinGecko."""
    try:
        url = (
            f"{COINGECKO_BASE}/coins/{coin_id}"
            f"?localization=false&tickers=true&community_data=true&developer_data=true"
        )
        data = _fetch(url)
        if not data or "market_data" not in data:
            return {"_error": f"No data for coin_id '{coin_id}'"}
        md = data.get("market_data", {})
        dd = data.get("developer_data", {})
        cd = data.get("community_data", {})

        result = {
            "name": data.get("name"),
            "symbol": data.get("symbol", "").upper(),
            "categories": data.get("categories", []),
            "price": md.get("current_price", {}).get("usd"),
            "market_cap": md.get("market_cap", {}).get("usd"),
            "fdv": md.get("fully_diluted_valuation", {}).get("usd"),
            "volume_24h": md.get("total_volume", {}).get("usd"),
            "circulating_supply": md.get("circulating_supply"),
            "total_supply": md.get("total_supply"),
            "max_supply": md.get("max_supply"),
            "ath": md.get("ath", {}).get("usd"),
            "ath_date": md.get("ath_date", {}).get("usd"),
            "atl": md.get("atl", {}).get("usd"),
            "price_change_24h": md.get("price_change_percentage_24h"),
            "price_change_7d": md.get("price_change_percentage_7d"),
            "price_change_30d": md.get("price_change_percentage_30d"),
            "price_change_1y": md.get("price_change_percentage_1y"),
            "market_cap_change_24h": md.get("market_cap_change_percentage_24h"),
            "total_volume_to_market_cap": (
                md.get("total_volume", {}).get("usd", 0) / md.get("market_cap", {}).get("usd", 1)
                if md.get("market_cap", {}).get("usd")
                else None
            ),
            "developer_commits_4w": dd.get("commit_count_4_weeks"),
            "developer_stars": dd.get("star_count"),
            "developer_forks": dd.get("fork_count"),
            "developer_contributors_30d": dd.get("developer_contributors_4_weeks"),
            "twitter_followers": cd.get("twitter_followers"),
            "reddit_subscribers": cd.get("reddit_subscribers"),
            "telegram_users": cd.get("telegram_channel_user_count"),
            "_fetched_at": time.time(),
        }
        _record_freshness(f"coin_data_{coin_id}")
        return result
    except Exception as e:
        return {"_error": f"Coin data fetch failed: {e}"}


@cached(ttl_seconds=300)
@retry(max_attempts=3, delay=2)
def coin_market_chart(coin_id, days=90):
    """Fetch price and volume chart data."""
    try:
        url = f"{COINGECKO_BASE}/coins/{coin_id}/market_chart?vs_currency=usd&days={days}"
        data = _fetch(url)
        if not data:
            return None
        prices = data.get("prices", [])
        market_caps = data.get("market_caps", [])
        result = {
            "days": days,
            "current_price": prices[-1][1] if prices else None,
            "price_high_90d": max(p[1] for p in prices) if prices else None,
            "price_low_90d": min(p[1] for p in prices) if prices else None,
            "start_price": prices[0][1] if prices else None,
            "_fetched_at": time.time(),
        }
        _record_freshness(f"chart_{coin_id}_{days}d")
        return result
    except Exception as e:
        return {"_error": f"Chart data fetch failed: {e}"}


@cached(ttl_seconds=3600)
@retry(max_attempts=3, delay=2)
def defillama_protocol(slug):
    """Fetch protocol TVL data from DefiLlama."""
    try:
        data = _fetch(f"{DEFILLAMA_BASE}/protocol/{slug}")
        if not data or "tvl" not in data:
            return None
        tvl_history = data.get("tvl", [])
        current_tvl = tvl_history[-1].get("totalLiquidityUSD", 0) if tvl_history else 0
        result = {
            "name": data.get("name"),
            "symbol": data.get("symbol"),
            "current_tvl": current_tvl,
            "change_1d": data.get("change_1d"),
            "change_7d": data.get("change_7d"),
            "change_1m": data.get("change_1m"),
            "chain_tvls": data.get("chainTvls", {}),
            "_fetched_at": time.time(),
        }
        _record_freshness(f"tvl_{slug}")
        return result
    except Exception as e:
        return {"_error": f"DeFiLlama fetch failed: {e}"}


@cached(ttl_seconds=180)
@retry(max_attempts=3, delay=2)
def trending_coins():
    """Fetch trending coins from CoinGecko (sector flow indicator)."""
    try:
        data = _fetch(f"{COINGECKO_BASE}/search/trending")
        if not data or "coins" not in data:
            return None
        trending = []
        for item in data["coins"][:15]:
            c = item.get("item", {})
            trending.append({
                "name": c.get("name"),
                "symbol": c.get("symbol"),
                "market_cap_rank": c.get("market_cap_rank"),
                "price_btc": c.get("price_btc"),
                "score": c.get("score"),
            })
        _record_freshness("trending")
        return trending
    except Exception as e:
        return {"_error": f"Trending fetch failed: {e}"}


# ── Report Builder ─────────────────────────────────────────────────────────────

def build_report(coin, sector_mode=False, json_mode=False):
    """Build a comprehensive fundamental analysis report."""
    coin_id = COIN_IDS.get(coin.lower(), coin.lower())
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    report = {
        "timestamp": timestamp,
        "coin": coin,
        "coin_id": coin_id,
        "version": "3.0",
    }

    # 1. Fear & Greed
    report["fear_greed"] = fear_greed_index()
    _stale_warning("fear_greed", 120)

    # 2. Global market
    report["global"] = global_data()
    _stale_warning("global_data", 120)

    # 3. Coin fundamentals
    report["coin_fundamentals"] = coin_data(coin_id)
    _stale_warning(f"coin_data_{coin_id}", 60)

    # 4. Price chart (90d)
    report["chart_90d"] = coin_market_chart(coin_id, 90)

    # 5. Sector data (if requested)
    report["sector"] = None
    if sector_mode:
        report["trending"] = trending_coins()

    # 6. TVL if applicable (check known protocols)
    protocol_slug = PROTOCOL_SLUGS.get(coin.lower())
    report["tvl"] = defillama_protocol(protocol_slug) if protocol_slug else None

    # 7. Data freshness summary
    report["data_freshness"] = {}
    for src in list(DATA_FRESHNESS.keys()):
        report["data_freshness"][src] = f"{fresh_since(src)}m"

    if json_mode:
        return json.dumps(report, indent=2)

    return _format_human_report(report)


def _format_human_report(r):
    """Format the report for human reading."""
    fg = r.get("fear_greed")
    gl = r.get("global")
    cf = r.get("coin_fundamentals", {}) or {}
    ch = r.get("chart_90d", {}) or {}
    tv = r.get("tvl", {}) or {}

    lines = []
    lines.append(f"Prometheus Data Report — {r['coin'].upper()} (v3.0)")
    lines.append(f"Generated: {r['timestamp']}")
    lines.append("")

    # Global market
    lines.append("── Global Market ──")
    if gl and "_error" not in gl:
        lines.append(f"  Total Market Cap : {_fmt(gl.get('total_market_cap'))}")
        lines.append(f"  24h Volume       : {_fmt(gl.get('total_volume_24h'))}")
        lines.append(f"  BTC Dominance    : {gl.get('btc_dominance', 'N/A'):.1f}%")
        lines.append(f"  ETH Dominance    : {gl.get('eth_dominance', 'N/A'):.1f}%")
        lines.append(f"  24h Market Chg   : {_pct(gl.get('market_cap_change_24h'))}")
    elif gl and "_error" in gl:
        lines.append(f"  {gl.get('_error')}")
    else:
        lines.append("  (unavailable)")
    lines.append("")

    # Fear & Greed
    lines.append("── Sentiment ──")
    if fg and "_error" not in fg:
        lines.append(f"  Fear & Greed     : {fg.get('value')} — {fg.get('classification')}")
        if fg.get("trend"):
            lines.append(f"  7-day trend      : {fg['trend']}")
    elif fg and "_error" in fg:
        lines.append(f"  {fg.get('_error')}")
    else:
        lines.append("  (unavailable)")
    lines.append("")

    # Coin fundamentals
    lines.append(f"── {cf.get('name', r['coin'].upper())} Fundamentals ──")
    if "_error" not in cf:
        lines.append(f"  Price            : {_fmt(cf.get('price'))}")
        lines.append(f"  Market Cap       : {_fmt(cf.get('market_cap'))}")
        lines.append(f"  FDV              : {_fmt(cf.get('fdv'))}")
        lines.append(f"  24h Volume       : {_fmt(cf.get('volume_24h'))}")
        lines.append(f"  Vol/MC Ratio     : {cf.get('total_volume_to_market_cap', 'N/A'):.3f}")
        lines.append(f"  24h Change       : {_pct(cf.get('price_change_24h'))}")
        lines.append(f"  7d Change        : {_pct(cf.get('price_change_7d'))}")
        lines.append(f"  30d Change       : {_pct(cf.get('price_change_30d'))}")
        lines.append(f"  1y Change        : {_pct(cf.get('price_change_1y'))}")
        lines.append(f"  ATH              : {_fmt(cf.get('ath'))} ({str(cf.get('ath_date', ''))[:10]})")
        lines.append(f"  ATL              : {_fmt(cf.get('atl'))}")
        lines.append(f"  Supply Circ      : {_fmt(cf.get('circulating_supply'), ' tokens')}")
        lines.append(f"  Supply Max       : {cf.get('max_supply') or 'Unlimited'}")
        lines.append(f"  Developers (4w)  : {cf.get('developer_commits_4w', 'N/A')} commits")
        lines.append(f"  GitHub Stars     : {cf.get('developer_stars', 'N/A')}")
        lines.append(f"  Twitter Followers: {_fmt(cf.get('twitter_followers'), '')}")
    else:
        lines.append(f"  {cf.get('_error')}")
    lines.append("")

    # Chart data
    lines.append("── 90-Day Price Context ──")
    if ch and "_error" not in ch:
        lines.append(f"  Current          : {_fmt(ch.get('current_price'))}")
        lines.append(f"  90d High         : {_fmt(ch.get('price_high_90d'))}")
        lines.append(f"  90d Low          : {_fmt(ch.get('price_low_90d'))}")
        lines.append(f"  90d Open         : {_fmt(ch.get('start_price'))}")
    elif ch and "_error" in ch:
        lines.append(f"  {ch.get('_error')}")
    else:
        lines.append("  (unavailable)")
    lines.append("")

    # TVL if applicable
    if tv and "_error" not in tv:
        lines.append(f"── {tv.get('name', 'Protocol')} TVL ──")
        lines.append(f"  TVL              : {_fmt(tv.get('current_tvl'))}")
        lines.append(f"  1d Change        : {_pct(tv.get('change_1d'))}")
        lines.append(f"  7d Change        : {_pct(tv.get('change_7d'))}")
        lines.append(f"  1m Change        : {_pct(tv.get('change_1m'))}")
        lines.append("")
    elif tv and "_error" in tv:
        lines.append(f"── TVL ──")
        lines.append(f"  {tv.get('_error')}")
        lines.append("")

    # Categories
    categories = cf.get("categories", [])
    if categories:
        lines.append(f"  Categories       : {', '.join(categories[:8])}")
        lines.append("")

    # Trending (sector context)
    if r.get("trending"):
        lines.append("── Trending Coins (Sector Flow) ──")
        for i, t in enumerate(r["trending"][:8], 1):
            lines.append(f"  {i}. {t.get('name')} ({t.get('symbol')}) — Rank #{t.get('market_cap_rank', '?')}")
        lines.append("")

    # Data freshness
    df = r.get("data_freshness", {})
    if df:
        lines.append("── Data Freshness ──")
        for src, age in sorted(df.items()):
            lines.append(f"  {src:30s}: {age}")
        lines.append("")

    return "\n".join(lines)


# ── CLI ────────────────────────────────────────────────────────────────────────


def list_coins():
    """Print all known coin mappings."""
    print("Known Coin IDs (CoinGecko):")
    for name, cid in sorted(COIN_IDS.items()):
        print(f"  {name:15s} -> {cid}")
    print()
    print("CoinGecko IDs accepted as-is. Unknown names passed directly.")
    print("Use --coin <id> where id is a CoinGecko coin ID.")


def main():
    parser = argparse.ArgumentParser(
        description="Prometheus — Fundamental Data Fetcher",
    )
    parser.add_argument(
        "--coin", "-c",
        default="bitcoin",
        help="Coin name or ID (default: bitcoin). See --list-coins.",
    )
    parser.add_argument(
        "--sector", "-s",
        action="store_true",
        help="Include sector/trending data for capital flow context.",
    )
    parser.add_argument(
        "--json", "-j",
        action="store_true",
        help="Output as JSON instead of human-readable report.",
    )
    parser.add_argument(
        "--list-coins",
        action="store_true",
        help="List known coin ID mappings.",
    )
    args = parser.parse_args()

    if args.list_coins:
        list_coins()
        return

    report = build_report(coin=args.coin, sector_mode=args.sector, json_mode=args.json)
    print(report)


if __name__ == "__main__":
    main()
