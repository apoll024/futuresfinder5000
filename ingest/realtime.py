"""
Real-time market data ingestion via Alpaca WebSocket.
Streams 1-minute bars, writes to PostgreSQL, triggers LLM analysis.

Resilience:
  - Exponential backoff reconnect on WebSocket disconnect (5s → 120s)
  - Per-symbol circuit breaker: backs off 15 min after 3 consecutive LLM failures
  - Rate limit: one analysis per symbol per 55 seconds (debounce rapid bars)
  - Graceful shutdown on SIGTERM / SIGINT
"""
import os, sys, time, signal
from datetime import datetime, time as dtime
from collections import defaultdict
from pathlib import Path
from zoneinfo import ZoneInfo
sys.path.insert(0, str(Path(__file__).parent.parent))

from alpaca.data.live import StockDataStream
from db.models import init_db, Session, Bar

API_KEY    = os.getenv("ALPACA_API_KEY")
SECRET_KEY = os.getenv("ALPACA_SECRET_KEY")
SYMBOLS    = [s.strip() for s in os.getenv("SYMBOLS", "TQQQ,SQQQ,UPRO,SPXU,SOXL,SOXS,QQQ,SPY,SOXX").split(",")]
ET         = ZoneInfo("America/New_York")

# Circuit breaker state per symbol
_fail_counts:   dict = defaultdict(int)
_backoff_until: dict = defaultdict(float)
_last_analyzed: dict = defaultdict(float)

CIRCUIT_OPEN_AFTER = 3      # failures before backing off
BACKOFF_SECONDS    = 900    # 15 min
MIN_ANALYSIS_GAP   = 55     # seconds between analyses per symbol (avoid hammering LLM)

_shutdown = False


def _handle_exit(signum, frame):
    global _shutdown
    print("[ingest] Signal received — shutting down gracefully")
    _shutdown = True

signal.signal(signal.SIGTERM, _handle_exit)
signal.signal(signal.SIGINT,  _handle_exit)


async def bar_handler(bar):
    if _shutdown:
        return

    # Write bar to DB
    try:
        session = Session()
        row = Bar(symbol=bar.symbol, ts=bar.timestamp,
                  open=bar.open, high=bar.high, low=bar.low,
                  close=bar.close, volume=bar.volume)
        session.merge(row)
        session.commit()
        session.close()
        print(f"[{bar.symbol}] {bar.timestamp}  C={bar.close:.2f}  V={int(bar.volume)}")
    except Exception as e:
        print(f"  [bar_handler] DB write error for {bar.symbol}: {e}")
        return

    now = time.time()

    # Rate limit — one analysis per symbol per minute
    if now - _last_analyzed[bar.symbol] < MIN_ANALYSIS_GAP:
        return

    # Circuit breaker
    if now < _backoff_until[bar.symbol]:
        remaining = int(_backoff_until[bar.symbol] - now)
        print(f"  [{bar.symbol}] Circuit open — {remaining}s remaining")
        return

    _last_analyzed[bar.symbol] = now

    try:
        from analyze.analyze import run_analysis
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


def run_stream():
    stream = StockDataStream(API_KEY, SECRET_KEY)
    stream.subscribe_bars(bar_handler, *SYMBOLS)
    print(f"[ingest] Stream connected | symbols: {SYMBOLS}")
    stream.run()


def main():
    print("[ingest] Starting historical backfill...")
    try:
        from ingest.historical import ingest_historical
        ingest_historical(symbols_override=SYMBOLS)
    except Exception as e:
        print(f"[ingest] Historical backfill error (non-fatal): {e}")

    init_db()

    backoff = 5
    max_backoff = 120
    errors = 0

    while not _shutdown:
        try:
            run_stream()
            print("[ingest] Stream ended cleanly — reconnecting in 10s...")
            backoff = 5
            errors  = 0
            time.sleep(10)
        except Exception as e:
            errors += 1
            print(f"[ingest] Stream error #{errors}: {e} — retrying in {backoff}s")
            time.sleep(backoff)
            backoff = min(backoff * 2, max_backoff)

    print("[ingest] Shutdown complete")


if __name__ == "__main__":
    main()
