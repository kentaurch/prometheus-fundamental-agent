#!/usr/bin/env python3
"""
project_valuation.py — DCF-Style Valuation Model for Prometheus v3.0

Estimates fair value of crypto projects using discounted cash flow methodology,
network revenue analysis, fee burn mechanics, inflation rates, and staking yields.

Usage:
    python3 project_valuation.py --coin bitcoin
    python3 project_valuation.py --coin ethereum --json
    python3 project_valuation.py --coin solana --risk-free 4.5

Output: Over/under valued signal with fair value range.

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
}

# Market parameters (used as defaults when live data unavailable)
DEFAULT_RISK_FREE_RATE = 4.25  # Approx US 10yr yield %
DEFAULT_CRYPTO_RISK_PREMIUM = 8.0  # Additional premium for crypto risk
DEFAULT_DISCOUNT_RATE = 12.0  # WACC-style discount for crypto projects

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
def coin_data(coin_id):
    """Fetch project data from CoinGecko for valuation inputs."""
    try:
        url = f"{COINGECKO_BASE}/coins/{coin_id}?localization=false&tickers=true&community_data=true&developer_data=true"
        data = _fetch(url)
        if not data or "market_data" not in data:
            return {"_error": f"No data for '{coin_id}'"}
        md = data.get("market_data", {})
        return {
            "name": data.get("name"),
            "symbol": data.get("symbol", "").upper(),
            "price": md.get("current_price", {}).get("usd"),
            "market_cap": md.get("market_cap", {}).get("usd"),
            "fdv": md.get("fully_diluted_valuation", {}).get("usd"),
            "volume_24h": md.get("total_volume", {}).get("usd"),
            "circulating_supply": md.get("circulating_supply"),
            "total_supply": md.get("total_supply"),
            "max_supply": md.get("max_supply"),
            "ath": md.get("ath", {}).get("usd"),
            "price_change_24h": md.get("price_change_percentage_24h"),
            "price_change_7d": md.get("price_change_percentage_7d"),
            "price_change_30d": md.get("price_change_percentage_30d"),
            "price_change_1y": md.get("price_change_percentage_1y"),
            "total_volume_to_market_cap": (
                md.get("total_volume", {}).get("usd", 0) / md.get("market_cap", {}).get("usd", 1)
                if md.get("market_cap", {}).get("usd") else None
            ),
            "_fetched_at": time.time(),
        }
    except Exception as e:
        return {"_error": f"Data fetch failed: {e}"}


# ── Valuation Models ────────────────────────────────────────────────────────────


class ValuationModel:
    """
    DCF-style valuation model for crypto assets.
    
    Three approaches:
    1. Network Revenue Model — values based on fee generation vs market cap
    2. Stock-to-Flow Model — values based on scarcity (primarily BTC)
    3. Yield Comparison Model — values based on staking yield vs risk-free
    """
    
    def __init__(self, coin_id, coin_data, risk_free_rate=None):
        self.coin_id = coin_id
        self.data = coin_data
        self.risk_free_rate = risk_free_rate if risk_free_rate is not None else DEFAULT_RISK_FREE_RATE
        self.discount_rate = self.risk_free_rate + DEFAULT_CRYPTO_RISK_PREMIUM
        
        # Valuation parameters
        self.fee_burn_annual = None  # Annualized fee burn / revenue
        self.inflation_rate = None
        self.staking_yield = None
    
    def _estimate_inflation_rate(self):
        """Estimate annual inflation rate from supply data."""
        total = self.data.get("total_supply")
        circ = self.data.get("circulating_supply")
        max_s = self.data.get("max_supply")
        
        if not total or not circ:
            return 2.0  # Default estimate
        
        # If max supply is fixed (like BTC), inflation is predictable
        if max_s and max_s > 0:
            remaining = max_s - circ
            if remaining > 0 and circ > 0:
                # Assume remaining unlocked over ~10 years
                return (remaining / circ) / 10 * 100
            return 0.0  # Fully diluted
        
        # If no max supply (like ETH pre-merge estimate), estimate from supply growth
        return 1.5  # Conservative estimate for uncapped assets
    
    def _estimate_network_revenue(self):
        """Estimate annualized network revenue from volume and fees."""
        vol_24h = self.data.get("volume_24h", 0) or 0
        price = self.data.get("price", 0) or 0
        
        if vol_24h == 0 or price == 0:
            return None
        
        # Estimate annualized fee revenue:
        # Assume avg fee of ~0.1-0.3% of volume depending on chain
        # BTC: ~0.1%, ETH: ~0.2%, others: ~0.3%
        if self.coin_id in ("bitcoin", "btc"):
            fee_rate = 0.001
        elif self.coin_id in ("ethereum", "eth"):
            fee_rate = 0.002
        else:
            fee_rate = 0.003
        
        daily_fees = vol_24h * fee_rate
        annual_fees = daily_fees * 365
        return annual_fees
    
    def _network_revenue_valuation(self):
        """Value based on Price-to-Sales (network fees) ratio."""
        annual_revenue = self._estimate_network_revenue()
        market_cap = self.data.get("market_cap", 0) or 0
        
        if not annual_revenue or annual_revenue == 0 or market_cap == 0:
            return None
        
        ps_ratio = market_cap / annual_revenue
        
        # Crypto-specific P/S multiples by asset type
        # BTC: 20-40x, ETH: 15-30x, others: 5-20x
        if self.coin_id in ("bitcoin", "btc"):
            fair_ps_low, fair_ps_high = 20, 40
        elif self.coin_id in ("ethereum", "eth"):
            fair_ps_low, fair_ps_high = 15, 30
        else:
            fair_ps_low, fair_ps_high = 5, 20
        
        fair_mcap_low = annual_revenue * fair_ps_low
        fair_mcap_high = annual_revenue * fair_ps_high
        
        circ_supply = self.data.get("circulating_supply", 1) or 1
        fair_price_low = fair_mcap_low / circ_supply
        fair_price_high = fair_mcap_high / circ_supply
        
        return {
            "method": "Network Revenue (P/S)",
            "annualized_revenue": annual_revenue,
            "current_ps_ratio": round(ps_ratio, 2),
            "fair_ps_range": [fair_ps_low, fair_ps_high],
            "fair_price_range": [round(fair_price_low, 4), round(fair_price_high, 4)],
            "fair_mcap_range": [fair_mcap_low, fair_mcap_high],
        }
    
    def _yield_comparison_valuation(self):
        """Compare staking yield / protocol yield vs risk-free rate."""
        price = self.data.get("price", 0) or 0
        market_cap = self.data.get("market_cap", 0) or 0
        
        # Estimate staking yield
        if self.coin_id in ("ethereum", "eth"):
            staking_yield = 3.5  # ~3.5% post-merge
        elif self.coin_id in ("solana", "sol"):
            staking_yield = 6.0  # ~6-7% SOL staking
        elif self.coin_id in ("cardano", "ada"):
            staking_yield = 3.0  # ~3% ADA staking
        elif self.coin_id in ("avalanche", "avax"):
            staking_yield = 8.0  # ~8% AVAX staking
        elif self.coin_id in ("polkadot", "dot"):
            staking_yield = 12.0  # ~12% DOT staking (includes inflation)
        else:
            staking_yield = 0.0  # Non-staking asset
        
        inflation = self._estimate_inflation_rate()
        net_real_yield = staking_yield - inflation
        
        # Fair value assessment based on real yield vs risk-free + risk premium
        required_yield = self.risk_free_rate + DEFAULT_CRYPTO_RISK_PREMIUM
        
        if net_real_yield >= required_yield:
            verdict = "Undervalued"
        elif net_real_yield >= self.risk_free_rate + 3:
            verdict = "Fair"
        else:
            verdict = "Overvalued"
        
        return {
            "method": "Yield Comparison",
            "estimated_staking_yield_pct": staking_yield,
            "estimated_inflation_pct": round(inflation, 2),
            "net_real_yield_pct": round(net_real_yield, 2),
            "risk_free_rate_pct": self.risk_free_rate,
            "required_yield_pct": required_yield,
            "verdict": verdict,
        }
    
    def _scarcity_valuation(self):
        """Stock-to-Flow based valuation (primarily for BTC)."""
        if self.coin_id not in ("bitcoin", "btc"):
            # Simplified: use FDV/Mcap ratio as scarcity proxy
            mcap = self.data.get("market_cap", 0) or 0
            fdv = self.data.get("fdv", 0) or 0
            if fdv > 0 and mcap > 0:
                dilution_ratio = mcap / fdv
                return {
                    "method": "Supply Dilution Analysis",
                    "fdv_to_mcap_ratio": round(dilution_ratio, 4),
                    "note": "Higher ratio = less future dilution pressure",
                    "assessment": "Low dilution" if dilution_ratio > 0.8 else "Moderate dilution" if dilution_ratio > 0.5 else "High future dilution",
                }
            return None
        
        # BTC-specific: simplified S2F assessment
        # Current BTC stock: ~19.5M, annual flow: ~164K (post-halving)
        # S2F ≈ 119, implying ~$100K-$1M valuation by S2F model
        price = self.data.get("price", 0) or 0
        circ = self.data.get("circulating_supply", 0) or 0
        max_s = self.data.get("max_supply", 0) or 0
        
        if price > 0 and circ > 0:
            remaining = max_s - circ if max_s > circ else 0
            years_to_full_dilution = remaining / (164250) if remaining > 0 else 0  # ~164K BTC/year post-halving
            s2f_ratio = circ / 164250 if circ > 0 else 0
            
            return {
                "method": "Stock-to-Flow (Simplified)",
                "current_supply": circ,
                "annual_flow_est": 164250,
                "s2f_ratio": round(s2f_ratio, 0),
                "years_to_full_dilution": round(years_to_full_dilution, 1),
                "note": "S2F model suggests price in $55K-$110K range at current S2F ratio (approx)",
            }
        return None
    
    def run_all(self):
        """Run all applicable valuation models and synthesize results."""
        results = {
            "asset": self.data.get("name", self.coin_id),
            "symbol": self.data.get("symbol", ""),
            "current_price": self.data.get("price"),
            "market_cap": self.data.get("market_cap"),
        }
        
        models_run = []
        
        # Model 1: Network Revenue (P/S)
        rev_val = self._network_revenue_valuation()
        if rev_val:
            models_run.append(rev_val)
        
        # Model 2: Yield Comparison
        yield_val = self._yield_comparison_valuation()
        if yield_val:
            models_run.append(yield_val)
        
        # Model 3: Scarcity / S2F
        scarcity_val = self._scarcity_valuation()
        if scarcity_val:
            models_run.append(scarcity_val)
        
        results["models"] = models_run
        
        # Synthesize final verdict
        results["synthesis"] = self._synthesize(models_run)
        
        return results
    
    def _synthesize(self, models):
        """Synthesize multiple valuation models into a single verdict."""
        if not models:
            return {
                "verdict": "INSUFFICIENT DATA",
                "conviction": 0,
                "summary": "Could not run any valuation models. Check data availability.",
            }
        
        signals = []
        price = self.data.get("price", 0) or 0
        undervalued_count = 0
        overvalued_count = 0
        fair_count = 0
        
        for model in models:
            method = model.get("method", "")
            if "Network Revenue" in method:
                fair_range = model.get("fair_price_range", [0, 0])
                if fair_range and len(fair_range) == 2:
                    if price < fair_range[0]:
                        signals.append(f"Undervalued vs Network Revenue model (price ${price:,.2f} < fair ${fair_range[0]:,.2f})")
                        undervalued_count += 1
                    elif price > fair_range[1]:
                        signals.append(f"Overvalued vs Network Revenue model (price ${price:,.2f} > fair ${fair_range[1]:,.2f})")
                        overvalued_count += 1
                    else:
                        signals.append(f"Fair vs Network Revenue model (price ${price:,.2f} within range ${fair_range[0]:,.2f}-${fair_range[1]:,.2f})")
                        fair_count += 1
            
            elif "Yield Comparison" in method:
                verdict = model.get("verdict", "")
                if verdict == "Undervalued":
                    signals.append(f"Undervalued: net real yield ({model.get('net_real_yield_pct', 0):.1f}%) > required ({model.get('required_yield_pct', 0):.1f}%)")
                    undervalued_count += 1
                elif verdict == "Overvalued":
                    signals.append(f"Overvalued: net real yield insufficient vs risk-free rate")
                    overvalued_count += 1
                else:
                    signals.append(f"Fair: net real yield adequate vs risk-free rate")
                    fair_count += 1
        
        # Final determination
        total = undervalued_count + overvalued_count + fair_count
        if total == 0:
            final_verdict = "INCONCLUSIVE"
            conviction = 0
        elif undervalued_count > overvalued_count and undervalued_count >= fair_count:
            final_verdict = "UNDERVALUED"
            conviction = min(undervalued_count * 3, 10)
        elif overvalued_count > undervalued_count and overvalued_count >= fair_count:
            final_verdict = "OVERVALUED"
            conviction = min(overvalued_count * 3, 10)
        else:
            final_verdict = "FAIR"
            conviction = 5
        
        # Adjust conviction based on data quality
        if self.data.get("_error"):
            conviction = max(conviction - 3, 0)
        
        return {
            "verdict": final_verdict,
            "conviction": conviction,
            "signals": signals,
            "models_run": len(models),
            "models_agreeing_undervalued": undervalued_count,
            "models_agreeing_overvalued": overvalued_count,
            "models_agreeing_fair": fair_count,
        }


# ── Report Builder ─────────────────────────────────────────────────────────────


def build_valuation_report(coin, risk_free_rate=None, json_mode=False):
    """Build comprehensive valuation report for a coin."""
    coin_id = COIN_IDS.get(coin.lower(), coin.lower())
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    
    # Fetch live data
    data = coin_data(coin_id)
    
    report = {
        "timestamp": timestamp,
        "coin": coin,
        "coin_id": coin_id,
        "version": "3.0",
        "assumptions": {
            "risk_free_rate_pct": risk_free_rate if risk_free_rate else DEFAULT_RISK_FREE_RATE,
            "crypto_risk_premium_pct": DEFAULT_CRYPTO_RISK_PREMIUM,
            "effective_discount_rate_pct": (risk_free_rate if risk_free_rate else DEFAULT_RISK_FREE_RATE) + DEFAULT_CRYPTO_RISK_PREMIUM,
        },
    }
    
    # Run valuation
    model = ValuationModel(coin_id, data, risk_free_rate)
    report["valuation"] = model.run_all()
    
    if json_mode:
        return json.dumps(report, indent=2)
    
    return _format_human_report(report)


def _format_human_report(r):
    """Format valuation report for human reading."""
    val = r.get("valuation", {})
    assumptions = r.get("assumptions", {})
    
    lines = []
    lines.append(f"Prometheus Project Valuation — {r['coin'].upper()} (v3.0)")
    lines.append(f"Generated: {r['timestamp']}")
    lines.append("")
    
    lines.append("── Assumptions ──")
    lines.append(f"  Risk-Free Rate      : {assumptions.get('risk_free_rate_pct', 'N/A')}%")
    lines.append(f"  Crypto Risk Premium : {assumptions.get('crypto_risk_premium_pct', 'N/A')}%")
    lines.append(f"  Discount Rate       : {assumptions.get('effective_discount_rate_pct', 'N/A')}%")
    lines.append("")
    
    lines.append(f"── {val.get('symbol', r['coin'].upper())} Snapshot ──")
    lines.append(f"  Current Price       : {_fmt(val.get('current_price'))}")
    lines.append(f"  Market Cap          : {_fmt(val.get('market_cap'))}")
    lines.append("")
    
    models = val.get("models", [])
    for model in models:
        method = model.get("method", "Unknown")
        lines.append(f"── Model: {method} ──")
        
        if "Network Revenue" in method:
            lines.append(f"  Est. Annual Revenue  : {_fmt(model.get('annualized_revenue'))}")
            lines.append(f"  Current P/S Ratio    : {model.get('current_ps_ratio', 'N/A')}x")
            fair_range = model.get("fair_price_range", [])
            if fair_range:
                lines.append(f"  Fair Price Range     : {_fmt(fair_range[0])} — {_fmt(fair_range[1])}")
            fair_mcap = model.get("fair_mcap_range", [])
            if fair_mcap:
                lines.append(f"  Fair Market Cap      : {_fmt(fair_mcap[0])} — {_fmt(fair_mcap[1])}")
        
        elif "Yield Comparison" in method:
            lines.append(f"  Est. Staking Yield   : {model.get('estimated_staking_yield_pct', 'N/A')}%")
            lines.append(f"  Est. Inflation       : {model.get('estimated_inflation_pct', 'N/A')}%")
            lines.append(f"  Net Real Yield       : {model.get('net_real_yield_pct', 'N/A')}%")
            lines.append(f"  Required Yield       : {model.get('required_yield_pct', 'N/A')}%")
            lines.append(f"  Verdict              : {model.get('verdict', 'N/A')}")
        
        elif "Stock-to-Flow" in method or "Supply Dilution" in method:
            lines.append(f"  S2F Ratio            : {model.get('s2f_ratio', 'N/A')}")
            lines.append(f"  Years to Full Dilut  : {model.get('years_to_full_dilution', 'N/A')}")
            if model.get("note"):
                lines.append(f"  Note: {model.get('note')}")
            if model.get("assessment"):
                lines.append(f"  Assessment           : {model.get('assessment')}")
        
        lines.append("")
    
    # Synthesis
    synth = val.get("synthesis", {})
    lines.append("══ SYNTHESIS ══")
    lines.append(f"  Verdict              : {synth.get('verdict', 'N/A')}")
    lines.append(f"  Conviction           : {synth.get('conviction', 0)}/10")
    lines.append("")
    
    signals = synth.get("signals", [])
    if signals:
        lines.append("  Signals:")
        for s in signals:
            lines.append(f"    - {s}")
        lines.append("")
    
    lines.append(f"  Models Undervalued   : {synth.get('models_agreeing_undervalued', 0)}")
    lines.append(f"  Models Overvalued    : {synth.get('models_agreeing_overvalued', 0)}")
    lines.append(f"  Models Fair          : {synth.get('models_agreeing_fair', 0)}")
    lines.append("")
    
    return "\n".join(lines)


# ── CLI ────────────────────────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(
        description="Prometheus — Project Valuation Model",
    )
    parser.add_argument("--coin", "-c", default="bitcoin",
                        help="Coin name or ID (default: bitcoin).")
    parser.add_argument("--risk-free", "-r", type=float, default=None,
                        help="Risk-free rate %% (e.g. 4.25 for 4.25%%). Default: ~4.25%%.")
    parser.add_argument("--json", "-j", action="store_true",
                        help="Output as JSON.")
    args = parser.parse_args()

    report = build_valuation_report(coin=args.coin, risk_free_rate=args.risk_free, json_mode=args.json)
    print(report)


if __name__ == "__main__":
    main()
