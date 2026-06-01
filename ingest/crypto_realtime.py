"""
Real-time crypto data ingestion via Alpaca WebSocket.
Streams 1-minute bars for crypto pairs, writes to PostgreSQL, triggers LLM analysis.

Resilience:
  - Exponential backoff reconnect on WebSocket disconnect (5s → 120s)
  - Per-symbol circuit breaker: backs off 15 min after 3 consecutive LLM failures
  - Rate limit: one analysis per symbol per configurable cooldown
  - Graceful shutdown on SIGTERM / SIGINT
  - Crypto is 24/7 — no market-hours gate applied
"""
import os, sys, time, signal, threading
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from alpaca.data.live import CryptoDataStream
from sqlalchemy.dialects.postgresql import insert as pg_insert
from db.models import init_db, Session, Bar, get_setting

API_KEY    = os.getenv("ALPACA_API_KEY")
SECRET_KEY = os.getenv("ALPACA_SECRET_KEY")

# Circuit breaker state per symbol
_fail_counts:   dict = defaultdict(int)
_backoff_until: dict = defaultdict(float)
_last_analyzed: dict = defaultdict(float)

CIRCUIT_OPEN_AFTER = 3
BACKOFF_SECONDS    = 900    # 15 min
MIN_ANALYSIS_GAP   = int(os.getenv("CRYPTO_ANALYSIS_GAP_SECONDS", os.getenv("MIN_ANALYSIS_GAP_SECONDS", "180")))

_shutdown    = False
_stream_ref  = None
_known_syms: set = set()
ALPACA_STREAM_SYMBOLS = {
    s.strip().upper() for s in os.getenv(
        "ALPACA_CRYPTO_STREAM_SYMBOLS",
        "BTC/USD,ETH/USD,SOL/USD,XRP/USD,DOGE/USD,LTC/USD,BCH/USD,LINK/USD",
    ).split(",") if s.strip()
}


def _handle_exit(signum, frame):
    global _shutdown
    print("[crypto] Signal received — shutting down gracefully")
    _shutdown = True

signal.signal(signal.SIGTERM, _handle_exit)
signal.signal(signal.SIGINT,  _handle_exit)


async def bar_handler(bar):
    if _shutdown:
        return
    if get_setting("crypto_enabled", "true") != "true":
        return

    # Write bar to DB
    try:
        session = Session()
        stmt = pg_insert(Bar.__table__).values(
            symbol=bar.symbol, ts=bar.timestamp,
            open=bar.open, high=bar.high, low=bar.low,
            close=bar.close, volume=bar.volume,
        ).on_conflict_do_update(
            index_elements=["symbol", "ts"],
            set_={
                "open": bar.open,
                "high": bar.high,
                "low": bar.low,
                "close": bar.close,
                "volume": bar.volume,
            },
        )
        session.execute(stmt)
        session.commit()
        session.close()
        print(f"[{bar.symbol}] {bar.timestamp}  C={bar.close:.2f}  V={int(bar.volume)}")
    except Exception as e:
        print(f"  [bar_handler] DB write error for {bar.symbol}: {e}")
        return

    now = time.time()

    if now - _last_analyzed[bar.symbol] < MIN_ANALYSIS_GAP:
        return

    if now < _backoff_until[bar.symbol]:
        remaining = int(_backoff_until[bar.symbol] - now)
        print(f"  [{bar.symbol}] Circuit open — {remaining}s remaining")
        return

    _last_analyzed[bar.symbol] = now

    try:
        from analyze.crypto_analyze import run_analysis
        run_analysis(bar.symbol)
        _fail_counts[bar.symbol] = 0
    except Exception as e:
        _fail_counts[bar.symbol] += 1
        count = _fail_counts[bar.symbol]
        print(f"  [{bar.symbol}] Analysis error ({count}/{CIRCUIT_OPEN_AFTER}): {e}")
        if count >= CIRCUIT_OPEN_AFTER:
            _backoff_until[bar.symbol] = time.time() + BACKOFF_SECONDS
            _fail_counts[bar.symbol]   = 0
            print(f"  [{bar.symbol}] Circuit OPEN — pausing analysis for {BACKOFF_SECONDS//60} min")


def _normalize_crypto_sym(s: str) -> str:
    """Ensure Alpaca-required format: BTC/USD, not BTC."""
    s = s.strip().upper()
    if s and "/" not in s:
        s = s + "/USD"
    return s


def _get_active_symbols() -> list:
    try:
        raw = get_setting("crypto_symbols", "BTC/USD,ETH/USD,SOL/USD")
        return [_normalize_crypto_sym(s) for s in raw.split(",") if s.strip()]
    except Exception:
        return ["BTC/USD", "ETH/USD", "SOL/USD"]


def _streamable_symbols(symbols: list) -> list:
    skipped = [s for s in symbols if s not in ALPACA_STREAM_SYMBOLS]
    if skipped:
        print(f"[crypto] Alpaca stream skipping unsupported symbols: {skipped}")
    return [s for s in symbols if s in ALPACA_STREAM_SYMBOLS]


def backfill_bars(symbols: list, min_hours: int = 30):
    """
    Ensure each symbol has at least min_hours of 1-min bars in the DB.
    Fetches missing history from Alpaca's crypto historical REST API.
    This gives compute_indicators() enough data to calculate 1h timeframe indicators.
    """
    try:
        from alpaca.data.historical import CryptoHistoricalDataClient
        from alpaca.data.requests import CryptoBarsRequest
        from alpaca.data.timeframe import TimeFrame
        client = CryptoHistoricalDataClient(API_KEY, SECRET_KEY)
    except Exception as e:
        print(f"[crypto] Backfill skipped — Alpaca historical client unavailable: {e}")
        return

    cutoff = datetime.now(timezone.utc) - timedelta(hours=min_hours)

    for symbol in symbols:
        try:
            db = Session()
            count = db.query(Bar).filter(Bar.symbol == symbol, Bar.ts >= cutoff).count()
            oldest = db.query(Bar.ts).filter(Bar.symbol == symbol).order_by(Bar.ts.asc()).first()
            db.close()

            if count >= min_hours * 55:  # ≥55 bars/hour on average = enough data
                print(f"[crypto] {symbol}: {count} bars in window — no backfill needed")
                continue

            fetch_start = cutoff
            if oldest and oldest[0] and oldest[0].replace(tzinfo=timezone.utc) < cutoff:
                fetch_start = cutoff  # fill only missing window
            else:
                fetch_start = cutoff

            print(f"[crypto] {symbol}: only {count} bars — backfilling from Alpaca since {fetch_start.strftime('%Y-%m-%dT%H:%M')}Z")
            req = CryptoBarsRequest(
                symbol_or_symbols=symbol,
                timeframe=TimeFrame.Minute,
                start=fetch_start,
                end=datetime.now(timezone.utc) - timedelta(minutes=2),
            )
            bars = client.get_crypto_bars(req)
            df = bars.df

            if df is None or df.empty:
                print(f"[crypto] {symbol}: no historical bars returned")
                continue

            # Reset multi-index if present (symbol, timestamp) → just timestamp
            if isinstance(df.index, type(df.index)) and df.index.nlevels > 1:
                df = df.reset_index(level=0, drop=True)

            written = 0
            db = Session()
            try:
                from sqlalchemy.dialects.postgresql import insert as pg_insert
                from sqlalchemy import inspect as sa_inspect
                rows = []
                for ts, row in df.iterrows():
                    ts_utc = ts.to_pydatetime().replace(tzinfo=timezone.utc) if ts.tzinfo is None else ts.to_pydatetime()
                    rows.append({
                        "symbol": symbol, "ts": ts_utc,
                        "open":   float(row["open"]),  "high": float(row["high"]),
                        "low":    float(row["low"]),   "close": float(row["close"]),
                        "volume": float(row.get("volume", 0)),
                    })
                if rows:
                    stmt = pg_insert(Bar).values(rows).on_conflict_do_nothing(
                        index_elements=["symbol", "ts"]
                    )
                    result = db.execute(stmt)
                    written = result.rowcount if result.rowcount >= 0 else len(rows)
                    db.commit()
                print(f"[crypto] {symbol}: backfilled {written} bars ({len(rows)} fetched)")
            finally:
                db.close()

        except Exception as e:
            print(f"[crypto] Backfill error for {symbol}: {e}")


def run_stream():
    global _stream_ref, _known_syms

    active = _streamable_symbols(_get_active_symbols())
    _known_syms = set(active)
    stream = CryptoDataStream(API_KEY, SECRET_KEY)
    _stream_ref = stream
    stream.subscribe_bars(bar_handler, *active)
    print(f"[crypto] Stream connected | symbols: {active}")

    t = threading.Thread(target=_symbol_watcher, daemon=True)
    t.start()

    stream.run()


def _symbol_watcher():
    """Poll DB every 60s for new crypto symbols and subscribe them to the live stream."""
    global _known_syms
    while not _shutdown:
        time.sleep(60)
        if _stream_ref is None:
            continue
        try:
            db_syms = set(_streamable_symbols(_get_active_symbols()))
            new = db_syms - _known_syms
            if new:
                print(f"[crypto] New symbols detected: {new} — subscribing")
                _stream_ref.subscribe_bars(bar_handler, *new)
                _known_syms |= new
        except Exception as e:
            print(f"[crypto] Symbol watcher error: {e}")


def main():
    init_db()

    # Backfill historical bars so 1h indicators have enough data on startup
    try:
        symbols = _get_active_symbols()
        backfill_bars(symbols, min_hours=55)  # 55h gives 55+ 1h bars for EMA-50
    except Exception as e:
        print(f"[crypto] Startup backfill failed (non-fatal): {e}")

    backoff     = 5
    max_backoff = 120
    errors      = 0

    while not _shutdown:
        try:
            run_stream()
            print("[crypto] Stream ended cleanly — reconnecting in 10s...")
            backoff = 5
            errors  = 0
            time.sleep(10)
        except Exception as e:
            errors += 1
            print(f"[crypto] Stream error #{errors}: {e} — retrying in {backoff}s")
            time.sleep(backoff)
            backoff = min(backoff * 2, max_backoff)

    print("[crypto] Shutdown complete")


if __name__ == "__main__":
    main()
