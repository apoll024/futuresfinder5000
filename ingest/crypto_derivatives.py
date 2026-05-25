"""
Crypto derivatives visibility feed.

Polls free Binance USD-M futures endpoints for funding rates, open interest,
long/short ratios, and taker flow for the configured FF5000 crypto symbols.
These metrics are read-only market visibility; execution still happens through
Coinbase.
"""
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).parent.parent))
from db.models import CryptoDerivativeMetric, Session, get_setting, init_db

BINANCE_FAPI = "https://fapi.binance.com/fapi/v1"
BINANCE_DATA = "https://fapi.binance.com/futures/data"
HYPERLIQUID_INFO = "https://api.hyperliquid.xyz/info"
POLL_SECONDS = int(os.getenv("DERIVATIVES_POLL_SECONDS", "300"))

BINANCE_SYMBOLS = {
    "BTC/USD": "BTCUSDT",
    "ETH/USD": "ETHUSDT",
    "SOL/USD": "SOLUSDT",
    "BNB/USD": "BNBUSDT",
    "XRP/USD": "XRPUSDT",
    "ADA/USD": "ADAUSDT",
    "DOGE/USD": "DOGEUSDT",
    "AVAX/USD": "AVAXUSDT",
    "MATIC/USD": "MATICUSDT",
    "LINK/USD": "LINKUSDT",
    "NEAR/USD": "NEARUSDT",
    "ALGO/USD": "ALGOUSDT",
    "FET/USD": "FETUSDT",
    "RNDR/USD": "RNDRUSDT",
}


def normalize_symbol(symbol: str) -> str:
    symbol = (symbol or "").strip().upper()
    if symbol and "/" not in symbol:
        symbol = f"{symbol}/USD"
    return symbol


def get_active_symbols() -> list[str]:
    raw = get_setting("crypto_symbols", os.getenv("CRYPTO_SYMBOLS", "BTC/USD,ETH/USD,SOL/USD"))
    return [normalize_symbol(s) for s in raw.split(",") if s.strip()]


def _ms_to_dt(value) -> datetime | None:
    try:
        ms = int(value)
        if ms <= 0:
            return None
        return datetime.fromtimestamp(ms / 1000, timezone.utc).replace(tzinfo=None)
    except Exception:
        return None


def _get_json(url: str, **params):
    r = requests.get(url, params=params, timeout=10)
    r.raise_for_status()
    return r.json()


def fetch_hyperliquid_derivatives(symbol: str) -> dict:
    """Fallback derivatives snapshot from Hyperliquid public API."""
    symbol = normalize_symbol(symbol)
    base = symbol.split("/")[0]
    r = requests.post(HYPERLIQUID_INFO, json={"type": "metaAndAssetCtxs"}, timeout=10)
    r.raise_for_status()
    meta, ctxs = r.json()
    universe = meta.get("universe", [])
    for asset, ctx in zip(universe, ctxs):
        if asset.get("name") != base:
            continue
        funding_rate = float(ctx.get("funding", 0) or 0) * 100
        mark_price = float(ctx.get("markPx", 0) or 0)
        open_interest = float(ctx.get("openInterest", 0) or 0)
        return {
            "symbol": symbol,
            "exchange_symbol": base,
            "source": "hyperliquid_perps",
            "mapped": True,
            "ts": datetime.now(timezone.utc).replace(tzinfo=None),
            "funding_rate": round(funding_rate, 5),
            "mark_price": mark_price,
            "index_price": float(ctx.get("oraclePx", 0) or 0),
            "next_funding_time": None,
            "open_interest": open_interest,
            "open_interest_value": open_interest * mark_price if open_interest and mark_price else None,
            "open_interest_trend": None,
            "long_short_ratio": None,
            "long_account_pct": None,
            "short_account_pct": None,
            "taker_buy_sell_ratio": None,
            "taker_buy_volume": None,
            "taker_sell_volume": None,
            "raw": {"hyperliquid": ctx},
        }
    return {"symbol": symbol, "mapped": False, "source": "hyperliquid_perps"}


def _trend(current: float | None, previous: float | None, threshold: float = 0.002) -> str | None:
    if current is None or previous is None or previous == 0:
        return None
    if current > previous * (1 + threshold):
        return "rising"
    if current < previous * (1 - threshold):
        return "falling"
    return "flat"


def fetch_derivatives(symbol: str) -> dict:
    """Fetch one derivatives snapshot for a configured symbol."""
    symbol = normalize_symbol(symbol)
    exchange_symbol = BINANCE_SYMBOLS.get(symbol)
    if not exchange_symbol:
        return fetch_hyperliquid_derivatives(symbol)

    raw = {}
    try:
        premium = _get_json(f"{BINANCE_FAPI}/premiumIndex", symbol=exchange_symbol)
        raw["premiumIndex"] = premium

        open_interest = _get_json(f"{BINANCE_FAPI}/openInterest", symbol=exchange_symbol)
        raw["openInterest"] = open_interest
    except Exception as e:
        print(f"[derivatives] Binance core fetch failed for {symbol}, trying Hyperliquid: {e}")
        return fetch_hyperliquid_derivatives(symbol)

    oi_hist = []
    try:
        oi_hist = _get_json(
            f"{BINANCE_DATA}/openInterestHist",
            symbol=exchange_symbol,
            period="5m",
            limit=3,
        )
    except Exception as e:
        print(f"[derivatives] OI history unavailable for {symbol}: {e}")
    raw["openInterestHist"] = oi_hist

    long_short = []
    try:
        long_short = _get_json(
            f"{BINANCE_DATA}/globalLongShortAccountRatio",
            symbol=exchange_symbol,
            period="5m",
            limit=1,
        )
    except Exception as e:
        print(f"[derivatives] long/short ratio unavailable for {symbol}: {e}")
    raw["globalLongShortAccountRatio"] = long_short

    taker = []
    try:
        taker = _get_json(
            f"{BINANCE_DATA}/takerlongshortRatio",
            symbol=exchange_symbol,
            period="5m",
            limit=1,
        )
    except Exception as e:
        print(f"[derivatives] taker flow unavailable for {symbol}: {e}")
    raw["takerlongshortRatio"] = taker

    latest_oi = oi_hist[-1] if oi_hist else {}
    prev_oi = oi_hist[-2] if len(oi_hist) >= 2 else {}
    oi_current = float(latest_oi.get("sumOpenInterest", 0) or open_interest.get("openInterest", 0) or 0)
    oi_previous = float(prev_oi.get("sumOpenInterest", 0) or 0) if prev_oi else None
    mark_price = float(premium.get("markPrice", 0) or 0)
    oi_value = latest_oi.get("sumOpenInterestValue")
    if oi_value is not None:
        oi_value = float(oi_value)
    elif oi_current and mark_price:
        oi_value = oi_current * mark_price

    ls = long_short[-1] if long_short else {}
    tk = taker[-1] if taker else {}

    return {
        "symbol": symbol,
        "exchange_symbol": exchange_symbol,
        "source": "binance_futures",
        "mapped": True,
        "ts": datetime.now(timezone.utc).replace(tzinfo=None),
        "funding_rate": round(float(premium.get("lastFundingRate", 0) or 0) * 100, 5),
        "mark_price": float(premium.get("markPrice", 0) or 0),
        "index_price": float(premium.get("indexPrice", 0) or 0),
        "next_funding_time": _ms_to_dt(premium.get("nextFundingTime")),
        "open_interest": oi_current,
        "open_interest_value": oi_value,
        "open_interest_trend": _trend(oi_current, oi_previous),
        "long_short_ratio": float(ls.get("longShortRatio", 0) or 0) if ls else None,
        "long_account_pct": float(ls.get("longAccount", 0) or 0) * 100 if ls else None,
        "short_account_pct": float(ls.get("shortAccount", 0) or 0) * 100 if ls else None,
        "taker_buy_sell_ratio": float(tk.get("buySellRatio", 0) or 0) if tk else None,
        "taker_buy_volume": float(tk.get("buyVol", 0) or 0) if tk else None,
        "taker_sell_volume": float(tk.get("sellVol", 0) or 0) if tk else None,
        "raw": raw,
    }


def store_derivatives(snapshot: dict) -> CryptoDerivativeMetric | None:
    if not snapshot.get("mapped"):
        return None
    db = Session()
    try:
        row = CryptoDerivativeMetric(
            symbol=snapshot["symbol"],
            exchange_symbol=snapshot["exchange_symbol"],
            source=snapshot.get("source", "binance_futures"),
            ts=snapshot["ts"],
            funding_rate=snapshot.get("funding_rate"),
            mark_price=snapshot.get("mark_price"),
            index_price=snapshot.get("index_price"),
            next_funding_time=snapshot.get("next_funding_time"),
            open_interest=snapshot.get("open_interest"),
            open_interest_value=snapshot.get("open_interest_value"),
            open_interest_trend=snapshot.get("open_interest_trend"),
            long_short_ratio=snapshot.get("long_short_ratio"),
            long_account_pct=snapshot.get("long_account_pct"),
            short_account_pct=snapshot.get("short_account_pct"),
            taker_buy_sell_ratio=snapshot.get("taker_buy_sell_ratio"),
            taker_buy_volume=snapshot.get("taker_buy_volume"),
            taker_sell_volume=snapshot.get("taker_sell_volume"),
            raw=json.dumps(snapshot.get("raw", {})),
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        return row
    finally:
        db.close()


def row_to_dict(row: CryptoDerivativeMetric | None) -> dict:
    if not row:
        return {}
    return {
        "symbol": row.symbol,
        "exchange_symbol": row.exchange_symbol,
        "source": row.source,
        "ts": row.ts.isoformat() if row.ts else None,
        "funding_rate": row.funding_rate,
        "mark_price": row.mark_price,
        "index_price": row.index_price,
        "next_funding_time": row.next_funding_time.isoformat() if row.next_funding_time else None,
        "open_interest": row.open_interest,
        "open_interest_value": row.open_interest_value,
        "open_interest_trend": row.open_interest_trend,
        "long_short_ratio": row.long_short_ratio,
        "long_account_pct": row.long_account_pct,
        "short_account_pct": row.short_account_pct,
        "taker_buy_sell_ratio": row.taker_buy_sell_ratio,
        "taker_buy_volume": row.taker_buy_volume,
        "taker_sell_volume": row.taker_sell_volume,
    }


def latest_derivatives(symbol: str) -> dict:
    symbol = normalize_symbol(symbol)
    db = Session()
    try:
        row = (db.query(CryptoDerivativeMetric)
               .filter(CryptoDerivativeMetric.symbol == symbol)
               .order_by(CryptoDerivativeMetric.ts.desc())
               .first())
        return row_to_dict(row)
    finally:
        db.close()


def latest_for_symbols(symbols: list[str]) -> list[dict]:
    return [latest_derivatives(s) or {"symbol": normalize_symbol(s), "mapped": False} for s in symbols]


def fetch_and_store(symbol: str) -> dict:
    snapshot = fetch_derivatives(symbol)
    if snapshot.get("mapped"):
        row = store_derivatives(snapshot)
        return row_to_dict(row)
    return snapshot


def poll_once(symbols: list[str] = None) -> list[dict]:
    symbols = symbols or get_active_symbols()
    results = []
    for symbol in symbols:
        try:
            data = fetch_and_store(symbol)
            results.append(data)
            if data.get("mapped") is False:
                print(f"[derivatives] {symbol}: no Binance futures mapping")
            else:
                print(
                    f"[derivatives] {symbol}: funding={data.get('funding_rate')}% "
                    f"OI={data.get('open_interest_trend') or 'n/a'} "
                    f"L/S={data.get('long_short_ratio') or 'n/a'}"
                )
        except Exception as e:
            print(f"[derivatives] {symbol}: fetch/store error: {e}")
    return results


def main():
    init_db()
    print(f"[derivatives] Feed starting; poll interval={POLL_SECONDS}s")
    while True:
        poll_once()
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
