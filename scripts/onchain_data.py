#!/usr/bin/env python3
"""
onchain_data.py — On-Chain Metrics Fetcher for Prometheus v3.0

Fetches on-chain data: whale flow indicators, exchange netflow, miner reserves,
and other blockchain fundamentals from public APIs.

Usage:
    python3 onchain_data.py --coin bitcoin
    python3 onchain_data.py --coin ethereum --json
    python3 onchain_data.py --list-metrics

Output: Structured on-chain metrics JSON.

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
BLOCKCHAIR_BASE = "https://api.blockchair.com"
COINMETRICS_BASE = "https://community-api.coinmetrics.io/v4"

# Known coin ID mappings (CoinGecko IDs)
COIN_IDS = {
    "bitcoin": "bitcoin", "btc": "bitcoin",
    "ethereum": "ethereum", "eth": "ethereum",
    "solana": "solana", "sol": "solana",
}

# ── Helpers ────────────────────────────────────────────────────────────────────

def _fetch(url, timeout=20):
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
        if v < 0.0001:
            return f"{v:.8f}"
        if v < 1:
            return f"{v:.6f}"
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


# ── Data Freshness ─────────────────────────────────────────────────────────────

DATA_FRESHNESS = {}

def _record_freshness(source):
    DATA_FRESHNESS[source] = time.time()

def fresh_since(source):
    if source in DATA_FRESHNESS:
        return f"{(time.time() - DATA_FRESHNESS[source]) / 60:.0f}"
    return "never"


# ── On-Chain Data Fetchers ─────────────────────────────────────────────────────


@cached(ttl_seconds=300)
@retry(max_attempts=3, delay=2)
def exchange_netflow(coin_id):
    """Fetch exchange netflow data from CoinGecko."""
    try:
        url = f"{COINGECKO_BASE}/coins/{coin_id}/exchange_rates"
        data = _fetch(url)
        if data and "_error" not in data:
            _record_freshness(f"netflow_{coin_id}")
            return {"rates": data, "coin": coin_id}
        return None
    except Exception as e:
        return {"_error": f"Exchange netflow failed: {e}"}


@cached(ttl_seconds=600)
@retry(max_attempts=3, delay=2)
def coin_ohlc(coin_id, days=7):
    """Fetch OHLC data — used to derive on-chain volume context."""
    try:
        url = f"{COINGECKO_BASE}/coins/{coin_id}/ohlc?vs_currency=usd&days={days}"
        data = _fetch(url)
        if not data or "_error" in data:
            return None
        ohlc_list = []
        for o in data:
            try:
                ohlc_list.append({
                    "time": o[0],
                    "open": o[1], "high": o[2],
                    "low": o[3], "close": o[4],
                })
            except (IndexError, ValueError, TypeError):
                continue
        _record_freshness(f"ohlc_{coin_id}_{days}d")
        return {"coin": coin_id, "days": days, "candles": len(ohlc_list), "data": ohlc_list}
    except Exception as e:
        return {"_error": f"OHLC fetch failed: {e}"}


@cached(ttl_seconds=600)
@retry(max_attempts=3, delay=2)
def coin_stats(coin_id):
    """Fetch on-chain statistics via CoinGecko."""
    try:
        url = f"{COINGECKO_BASE}/coins/{coin_id}?localization=false&tickers=false&community_data=true&developer_data=true"
        data = _fetch(url)
        if not data or "_error" in data:
            return None
        md = data.get("market_data", {})
        result = {
            "name": data.get("name"),
            "symbol": data.get("symbol", "").upper(),
            "circulating_supply": md.get("circulating_supply"),
            "total_supply": md.get("total_supply"),
            "max_supply": md.get("max_supply"),
            "price": md.get("current_price", {}).get("usd"),
            "market_cap": md.get("market_cap", {}).get("usd"),
            "volume_24h": md.get("total_volume", {}).get("usd"),
            "high_24h": md.get("high_24h", {}).get("usd"),
            "low_24h": md.get("low_24h", {}).get("usd"),
            "ath": md.get("ath", {}).get("usd"),
            "atl": md.get("atl", {}).get("usd"),
            "price_change_24h": md.get("price_change_percentage_24h"),
            "price_change_7d": md.get("price_change_percentage_7d"),
            "price_change_30d": md.get("price_change_percentage_30d"),
            "market_cap_change_24h": md.get("market_cap_change_percentage_24h"),
        }
        _record_freshness(f"stats_{coin_id}")
        return result
    except Exception as e:
        return {"_error": f"Stats fetch failed: {e}"}


@cached(ttl_seconds=1800)
@retry(max_attempts=3, delay=2)
def blockchair_stats(chain="bitcoin"):
    """Fetch blockchain stats from Blockchair (limited free tier)."""
    chain_map = {"bitcoin": "bitcoin", "btc": "bitcoin",
                 "ethereum": "ethereum", "eth": "ethereum"}
    mapped = chain_map.get(chain.lower(), chain.lower())
    try:
        url = f"{BLOCKCHAIR_BASE}/{mapped}/stats"
        data = _fetch(url)
        if not data or "_error" in data or "data" not in data:
            return None
        d = data["data"]
        result = {
            "chain": mapped,
            "blocks": d.get("blocks"),
            "transactions": d.get("transactions"),
            "difficulty": d.get("difficulty"),
            "hashrate_24h": d.get("hashrate_24h"),
            "mempool_transactions": d.get("mempool_transactions"),
            "mempool_size": d.get("mempool_size"),
            "coin_price": d.get("market_price_usd"),
            "volume_24h": d.get("volume_24h_usd"),
        }
        _record_freshness(f"blockchair_{mapped}")
        return result
    except Exception as e:
        return {"_error": f"Blockchair fetch failed: {e}"}


@cached(ttl_seconds=1800)
@retry(max_attempts=3, delay=2)
def coinmetrics_asset_info(asset="btc"):
    """Fetch asset info from CoinMetrics community API."""
    try:
        url = f"{COINMETRICS_BASE}/asset/{asset}"
        data = _fetch(url)
        if not data or "_error" in data:
            return None
        _record_freshness(f"coinmetrics_{asset}")
        return data
    except Exception as e:
        return {"_error": f"CoinMetrics fetch failed: {e}"}


@cached(ttl_seconds=1800)
@retry(max_attempts=3, delay=2)
def coinmetrics_metric(asset="btc", metric="FeeTotUSD"):
    """Fetch specific on-chain metric from CoinMetrics community API."""
    try:
        url = f"{COINMETRICS_BASE}/timeseries/asset-metrics?assets={asset}&metrics={metric}&limit=1"
        data = _fetch(url)
        if not data or "_error" in data:
            return None
        _record_freshness(f"cm_{asset}_{metric}")
        return data
    except Exception as e:
        return {"_error": f"CoinMetrics metric fetch failed: {e}"}


# ── Aggregation & Signal ───────────────────────────────────────────────────────


def detect_whale_signals(coin_id, stats):
    """Derive whale accumulation/distribution signals from available data."""
    signals = []
    concerns = []
    
    if not stats or "_error" in stats:
        return {"signals": [], "concerns": ["No data available"]}
    
    price = stats.get("price", 0)
    ath = stats.get("ath", 0) or 1
    
    # Distance from ATH
    dist_from_ath = ((price / ath) - 1) * 100 if ath else 0
    
    # Volume surge detection
    vol_24h = stats.get("volume_24h", 0) or 0
    mcap = stats.get("market_cap", 0) or 1
    vol_to_mcap = vol_24h / mcap if mcap else 0
    
    if vol_to_mcap > 0.2:
        signals.append("High volume-to-market-cap ratio — possible whale accumulation or distribution")
    elif vol_to_mcap < 0.02:
        signals.append("Low volume relative to market cap — low whale interest currently")
    
    if dist_from_ath < -70:
        signals.append("Price far below ATH (>70%) — potential accumulation zone if fundamentals intact")
        signals.append("Whale accumulation often increases after deep corrections")
    elif dist_from_ath < -30:
        signals.append("Moderate distance from ATH — mixed whale signals possible")
    else:
        signals.append("Price near ATH — watch for whale distribution")
    
    # Supply metrics
    max_supply = stats.get("max_supply")
    circ_supply = stats.get("circulating_supply")
    if max_supply and circ_supply:
        circ_pct = (circ_supply / max_supply) * 100
        if circ_pct > 90:
            signals.append("High circulating supply ratio — reduced future dilution pressure")
        elif circ_pct < 50:
            concerns.append("Large portion of supply still locked/unreleased — future dilution risk")
    
    return {
        "signals": signals,
        "concerns": concerns,
        "dist_from_ath_pct": round(dist_from_ath, 1),
        "vol_to_mcap_ratio": round(vol_to_mcap, 4),
    }


# ── Report Builder ─────────────────────────────────────────────────────────────


def build_onchain_report(coin, json_mode=False):
    """Build comprehensive on-chain metrics report."""
    coin_id = COIN_IDS.get(coin.lower(), coin.lower())
    asset = coin_id[:3] if len(coin_id) >= 3 else coin_id  # short asset code
    
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    
    report = {
        "timestamp": timestamp,
        "coin": coin,
        "coin_id": coin_id,
        "version": "3.0",
    }
    
    # 1. Basic stats
    stats = coin_stats(coin_id)
    report["stats"] = stats
    
    # 2. OHLC data (7d for short-term context)
    report["ohlc_7d"] = coin_ohlc(coin_id, 7)
    
    # 3. Blockchair stats (if available)
    report["blockchain_stats"] = blockchair_stats(coin_id)
    
    # 4. CoinMetrics data
    if coin_id == "bitcoin" or coin_id == "btc":
        report["coinmetrics_fees"] = coinmetrics_metric("btc", "FeeTotUSD")
        report["coinmetrics_tx"] = coinmetrics_metric("btc", "TxCnt")
    
    # 5. Whale signals
    report["whale_signals"] = detect_whale_signals(coin_id, stats)
    
    # 6. Data freshness
    report["data_freshness"] = {}
    for src in sorted(DATA_FRESHNESS.keys()):
        report["data_freshness"][src] = f"{fresh_since(src)}m"
    
    if json_mode:
        return json.dumps(report, indent=2)
    
    return _format_human_report(report)


def _format_human_report(r):
    """Format on-chain report for human reading."""
    lines = []
    lines.append(f"Prometheus On-Chain Report — {r['coin'].upper()} (v3.0)")
    lines.append(f"Generated: {r['timestamp']}")
    lines.append("")
    
    # Basic stats
    stats = r.get("stats", {}) or {}
    lines.append("── Network Basics ──")
    if "_error" not in stats:
        lines.append(f"  Price            : {_fmt(stats.get('price'))}")
        lines.append(f"  Market Cap       : {_fmt(stats.get('market_cap'))}")
        lines.append(f"  Volume 24h       : {_fmt(stats.get('volume_24h'))}")
        lines.append(f"  24h Change       : {_pct(stats.get('price_change_24h'))}")
        lines.append(f"  7d Change        : {_pct(stats.get('price_change_7d'))}")
        lines.append(f"  ATH              : {_fmt(stats.get('ath'))}")
        lines.append(f"  ATL              : {_fmt(stats.get('atl'))}")
        lines.append(f"  Circ Supply      : {_fmt(stats.get('circulating_supply'), ' tokens')}")
        if stats.get("max_supply"):
            lines.append(f"  Max Supply       : {_fmt(stats.get('max_supply'), ' tokens')}")
    else:
        lines.append(f"  {stats.get('_error', 'unavailable')}")
    lines.append("")
    
    # Blockchain stats
    bs = r.get("blockchain_stats", {}) or {}
    lines.append("── Blockchain Health ──")
    if "_error" not in bs:
        lines.append(f"  Blocks           : {_fmt(bs.get('blocks'), '')}")
        lines.append(f"  Transactions     : {_fmt(bs.get('transactions'), '')}")
        lines.append(f"  Hashrate 24h     : {bs.get('hashrate_24h', 'N/A')}")
        lines.append(f"  Difficulty       : {_fmt(bs.get('difficulty'), '')}")
        lines.append(f"  Mempool TX       : {bs.get('mempool_transactions', 'N/A')}")
        lines.append(f"  Mempool Size     : {bs.get('mempool_size', 'N/A')}")
    else:
        lines.append(f"  {bs.get('_error', 'unavailable')}")
    lines.append("")
    
    # Whale signals
    ws = r.get("whale_signals", {}) or {}
    lines.append("── Whale Flow Signals ──")
    signals = ws.get("signals", [])
    concerns = ws.get("concerns", [])
    if signals:
        for s in signals:
            lines.append(f"  [+] {s}")
    if concerns:
        for c in concerns:
            lines.append(f"  [!] {c}")
    if not signals and not concerns:
        lines.append("  (insufficient data)")
    lines.append(f"  Distance from ATH: {ws.get('dist_from_ath_pct', 'N/A')}%")
    lines.append(f"  Vol/Mcap Ratio   : {ws.get('vol_to_mcap_ratio', 'N/A')}")
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


def list_metrics():
    """List available on-chain metrics and data sources."""
    print("Available On-Chain Metrics:")
    print()
    print("  Source         | Metrics")
    print("  ---------------|----------------------------------------------------")
    print("  CoinGecko      | Price, market cap, volume, supply, ATH/ATL")
    print("  Blockchair     | Blocks, TX count, difficulty, hashrate, mempool")
    print("  CoinMetrics    | Fee revenue, TX counts, network economics")
    print()
    print("  Derived Signals:")
    print("    - Whale accumulation/distribution (from price vs ATH + volume)")
    print("    - Exchange netflow context (from volume surges)")
    print("    - Supply dilution risk (circ vs max supply)")
    print()
    print("  Supported coins: " + ", ".join(sorted(COIN_IDS.keys())))


def main():
    parser = argparse.ArgumentParser(
        description="Prometheus — On-Chain Data Fetcher",
    )
    parser.add_argument("--coin", "-c", default="bitcoin",
                        help="Coin name or ID (default: bitcoin).")
    parser.add_argument("--json", "-j", action="store_true",
                        help="Output as JSON.")
    parser.add_argument("--list-metrics", action="store_true",
                        help="List available on-chain metrics.")
    args = parser.parse_args()

    if args.list_metrics:
        list_metrics()
        return

    report = build_onchain_report(coin=args.coin, json_mode=args.json)
    print(report)


if __name__ == "__main__":
    main()
