"""
Redundant 1-minute crypto market-data feeds.

Polls public Coinbase Exchange and Kraken 1-minute candles, optionally folds in
Alpaca REST bars and CoinMarketCap quotes when credentials are configured, stores
raw per-source bars, then writes a consensus bar into the primary bars table used
by signal analysis.
"""
import json
import os
import statistics
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from sqlalchemy.dialects.postgresql import insert as pg_insert

sys.path.insert(0, str(Path(__file__).parent.parent))
from db.models import Bar, FeedBar, Session, Signal, get_setting, init_db

POLL_SECONDS = int(os.getenv("REDUNDANT_FEED_POLL_SECONDS", "60"))
ANALYZE_FROM_FEEDS = os.getenv("REDUNDANT_FEED_ANALYZE", "true").lower() == "true"
CMC_API_KEY = os.getenv("CMC_API_KEY") or os.getenv("COINMARKETCAP_API_KEY", "")
ALPACA_API_KEY = os.getenv("ALPACA_API_KEY")
ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY")
COINBASE_PRODUCTS = {
    "BTC/USD": "BTC-USD",
    "ETH/USD": "ETH-USD",
    "SOL/USD": "SOL-USD",
    "XRP/USD": "XRP-USD",
    "XLM/USD": "XLM-USD",
    "ALGO/USD": "ALGO-USD",
    "ADA/USD": "ADA-USD",
    "DOGE/USD": "DOGE-USD",
    "LINK/USD": "LINK-USD",
    "AVAX/USD": "AVAX-USD",
}
KRAKEN_PAIRS = {
    "BTC/USD": "XBTUSD",
    "ETH/USD": "ETHUSD",
    "SOL/USD": "SOLUSD",
    "XRP/USD": "XRPUSD",
    "XLM/USD": "XLMUSD",
    "ALGO/USD": "ALGOUSD",
    "ADA/USD": "ADAUSD",
    "DOGE/USD": "DOGEUSD",
    "LINK/USD": "LINKUSD",
    "AVAX/USD": "AVAXUSD",
}


def _alpaca_client():
    if not ALPACA_API_KEY or not ALPACA_SECRET_KEY:
        return None
    try:
        from alpaca.data.historical import CryptoHistoricalDataClient
        return CryptoHistoricalDataClient(ALPACA_API_KEY, ALPACA_SECRET_KEY)
    except Exception as e:
        print(f"[feeds] alpaca client unavailable: {e}")
        return None


def _symbols() -> list[str]:
    raw = get_setting("crypto_symbols", os.getenv("CRYPTO_SYMBOLS", "BTC/USD,ETH/USD,SOL/USD"))
    symbols = []
    for s in raw.split(","):
        sym = s.strip().upper()
        if not sym:
            continue
        if "/" not in sym:
            sym = f"{sym}/USD"
        symbols.append(sym)
    return symbols


def _minute(dt: datetime) -> datetime:
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt.replace(second=0, microsecond=0)


def _bar(source: str, symbol: str, ts, open_, high, low, close, volume, raw=None) -> dict:
    return {
        "source": source,
        "symbol": symbol,
        "ts": _minute(ts if isinstance(ts, datetime) else datetime.fromtimestamp(float(ts), timezone.utc)),
        "open": float(open_),
        "high": float(high),
        "low": float(low),
        "close": float(close),
        "volume": float(volume or 0),
        "raw": json.dumps(raw or {}, separators=(",", ":"))[:4000],
    }


def fetch_coinbase(symbol: str, limit: int = 4) -> list[dict]:
    product = COINBASE_PRODUCTS.get(symbol)
    if not product:
        return []
    end = datetime.now(timezone.utc)
    start = end - timedelta(minutes=limit + 2)
    r = requests.get(
        f"https://api.exchange.coinbase.com/products/{product}/candles",
        params={"granularity": 60, "start": start.isoformat(), "end": end.isoformat()},
        timeout=10,
    )
    if not r.ok:
        print(f"[feeds] coinbase {symbol}: HTTP {r.status_code} {r.text[:120]}")
        return []
    rows = []
    for c in r.json()[:limit]:
        # Coinbase candle: [time, low, high, open, close, volume]
        rows.append(_bar("coinbase", symbol, c[0], c[3], c[2], c[1], c[4], c[5], {"product": product}))
    return rows


def fetch_kraken(symbol: str, limit: int = 4) -> list[dict]:
    pair = KRAKEN_PAIRS.get(symbol)
    if not pair:
        return []
    r = requests.get(
        "https://api.kraken.com/0/public/OHLC",
        params={"pair": pair, "interval": 1},
        timeout=10,
    )
    if not r.ok:
        print(f"[feeds] kraken {symbol}: HTTP {r.status_code} {r.text[:120]}")
        return []
    payload = r.json()
    if payload.get("error"):
        print(f"[feeds] kraken {symbol}: {payload.get('error')}")
        return []
    data = payload.get("result", {})
    key = next((k for k in data.keys() if k != "last"), None)
    rows = []
    for c in (data.get(key) or [])[-limit:]:
        # Kraken OHLC: [time, open, high, low, close, vwap, volume, count]
        rows.append(_bar("kraken", symbol, c[0], c[1], c[2], c[3], c[4], c[6], {"pair": pair}))
    return rows


def fetch_alpaca(symbol: str, limit: int = 4) -> list[dict]:
    client = _alpaca_client()
    if not client:
        return []
    try:
        from alpaca.data.requests import CryptoBarsRequest
        from alpaca.data.timeframe import TimeFrame
        end = datetime.now(timezone.utc) - timedelta(seconds=5)
        start = end - timedelta(minutes=limit + 2)
        req = CryptoBarsRequest(
            symbol_or_symbols=symbol,
            timeframe=TimeFrame.Minute,
            start=start,
            end=end,
        )
        bars = client.get_crypto_bars(req)
        df = bars.df
        if df is None or df.empty:
            return []
        if getattr(df.index, "nlevels", 1) > 1:
            df = df.reset_index(level=0, drop=True)
        rows = []
        for ts, row in df.tail(limit).iterrows():
            if hasattr(ts, "to_pydatetime"):
                ts = ts.to_pydatetime()
            rows.append(_bar(
                "alpaca", symbol, ts,
                row["open"], row["high"], row["low"], row["close"],
                row.get("volume", 0), {"feed": "alpaca_crypto_bars"}
            ))
        return rows
    except Exception as e:
        print(f"[feeds] alpaca {symbol}: {e}")
        return []


def fetch_coinmarketcap(symbols: list[str]) -> list[dict]:
    if not CMC_API_KEY:
        return []
    tickers = sorted({s.split("/")[0] for s in symbols})
    try:
        r = requests.get(
            "https://pro-api.coinmarketcap.com/v1/cryptocurrency/quotes/latest",
            headers={"X-CMC_PRO_API_KEY": CMC_API_KEY, "Accept": "application/json"},
            params={"symbol": ",".join(tickers), "convert": "USD"},
            timeout=12,
        )
        if not r.ok:
            print(f"[feeds] coinmarketcap: HTTP {r.status_code} {r.text[:160]}")
            return []
        data = r.json().get("data", {})
        now = _minute(datetime.utcnow())
        rows = []
        for symbol in symbols:
            base = symbol.split("/")[0]
            item = data.get(base)
            if isinstance(item, list):
                item = item[0] if item else None
            quote = (item or {}).get("quote", {}).get("USD", {})
            price = quote.get("price")
            if price is None:
                continue
            rows.append(_bar(
                "coinmarketcap", symbol, now, price, price, price, price,
                quote.get("volume_24h", 0), {"cmc_id": (item or {}).get("id")}
            ))
        return rows
    except Exception as e:
        print(f"[feeds] coinmarketcap error: {e}")
        return []


def _upsert_feed_bars(rows: list[dict]) -> int:
    if not rows:
        return 0
    with Session() as db:
        stmt = pg_insert(FeedBar.__table__).values(rows)
        stmt = stmt.on_conflict_do_update(
            index_elements=["source", "symbol", "ts"],
            set_={
                "open": stmt.excluded.open,
                "high": stmt.excluded.high,
                "low": stmt.excluded.low,
                "close": stmt.excluded.close,
                "volume": stmt.excluded.volume,
                "raw": stmt.excluded.raw,
            },
        )
        result = db.execute(stmt)
        db.commit()
        return result.rowcount or len(rows)


def _aggregate_symbol(symbol: str, minutes: int = 8) -> int:
    cutoff = datetime.utcnow().replace(second=0, microsecond=0) - timedelta(minutes=minutes)
    with Session() as db:
        rows = (db.query(FeedBar)
                  .filter(FeedBar.symbol == symbol, FeedBar.ts >= cutoff)
                  .order_by(FeedBar.ts.asc())
                  .all())
        by_ts = {}
        for row in rows:
            by_ts.setdefault(row.ts, []).append(row)

        consensus = []
        for ts, group in by_ts.items():
            usable = [g for g in group if g.close is not None and g.close > 0]
            if len(usable) < 2:
                continue
            closes = [g.close for g in usable]
            consensus.append({
                "symbol": symbol,
                "ts": ts,
                "open": statistics.median([g.open for g in usable if g.open is not None]),
                "high": max(g.high for g in usable if g.high is not None),
                "low": min(g.low for g in usable if g.low is not None),
                "close": statistics.median(closes),
                "volume": statistics.median([g.volume or 0 for g in usable]),
            })
        if not consensus:
            return 0
        stmt = pg_insert(Bar.__table__).values(consensus)
        stmt = stmt.on_conflict_do_update(
            index_elements=["symbol", "ts"],
            set_={
                "open": stmt.excluded.open,
                "high": stmt.excluded.high,
                "low": stmt.excluded.low,
                "close": stmt.excluded.close,
                "volume": stmt.excluded.volume,
            },
        )
        result = db.execute(stmt)
        db.commit()
        return result.rowcount or len(consensus)


def _latest_consensus_ts(symbol: str):
    with Session() as db:
        row = (db.query(Bar.ts)
                 .filter(Bar.symbol == symbol)
                 .order_by(Bar.ts.desc())
                 .first())
        return row[0] if row else None


def _latest_signal_age_seconds(symbol: str) -> float | None:
    with Session() as db:
        row = (db.query(Signal.ts)
                 .filter(Signal.symbol == symbol)
                 .order_by(Signal.ts.desc())
                 .first())
        if not row or not row[0]:
            return None
        return (datetime.utcnow() - row[0]).total_seconds()


def maybe_run_analysis(symbol: str) -> None:
    if not ANALYZE_FROM_FEEDS:
        return
    latest_bar = _latest_consensus_ts(symbol)
    if not latest_bar or (datetime.utcnow() - latest_bar).total_seconds() > 180:
        return
    age = _latest_signal_age_seconds(symbol)
    if age is not None and age < 55:
        return
    try:
        from analyze.crypto_analyze import run_analysis
        run_analysis(symbol)
    except Exception as e:
        print(f"[feeds] analysis error for {symbol}: {e}")


def poll_once() -> None:
    symbols = _symbols()
    rows = []
    for symbol in symbols:
        for fetcher in (fetch_alpaca, fetch_coinbase, fetch_kraken):
            try:
                rows.extend(fetcher(symbol))
            except Exception as e:
                print(f"[feeds] {fetcher.__name__} {symbol}: {e}")
    rows.extend(fetch_coinmarketcap(symbols))
    written = _upsert_feed_bars(rows)
    aggregated_by_symbol = {symbol: _aggregate_symbol(symbol) for symbol in symbols}
    aggregated = sum(aggregated_by_symbol.values())
    for symbol, count in aggregated_by_symbol.items():
        if count:
            maybe_run_analysis(symbol)
    source_counts = {}
    for row in rows:
        source_counts[row["source"]] = source_counts.get(row["source"], 0) + 1
    print(f"[feeds] raw={written} consensus={aggregated} sources={source_counts}")


def main():
    init_db()
    while True:
        started = time.time()
        try:
            poll_once()
        except Exception as e:
            print(f"[feeds] poll error: {e}")
        sleep_for = max(5, POLL_SECONDS - (time.time() - started))
        time.sleep(sleep_for)


if __name__ == "__main__":
    main()
