"""
Real-time market data ingestion via Alpaca WebSocket.
Streams 1-minute bars, writes to PostgreSQL, triggers LLM analysis.

Resilience:
  - Exponential backoff reconnect on WebSocket disconnect (5s → 120s)
  - Per-symbol circuit breaker: backs off 15 min after 3 consecutive LLM failures
  - Rate limit: one analysis per symbol per configurable cooldown
  - Graceful shutdown on SIGTERM / SIGINT
"""
import os, sys, time, signal, threading
from datetime import datetime, time as dtime
from collections import defaultdict
from pathlib import Path
from zoneinfo import ZoneInfo
sys.path.insert(0, str(Path(__file__).parent.parent))

from alpaca.data.live import StockDataStream
from sqlalchemy.dialects.postgresql import insert as pg_insert
from db.models import init_db, Session, Bar, get_setting

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
MIN_ANALYSIS_GAP   = int(os.getenv("STOCK_ANALYSIS_GAP_SECONDS", os.getenv("MIN_ANALYSIS_GAP_SECONDS", "180")))
MAX_ANALYSES_PER_WINDOW = int(os.getenv("STOCK_MAX_ANALYSES_PER_MINUTE", "4"))
ANALYSIS_WINDOW_SECONDS = 60

_shutdown    = False
_stream_ref  = None         # set in run_stream, used by symbol watcher
_analysis_window_start = 0.0
_analysis_window_count = 0
_known_syms: set = set()    # tracked set for dynamic additions


def _handle_exit(signum, frame):
    global _shutdown
    print("[ingest] Signal received — shutting down gracefully")
    _shutdown = True

signal.signal(signal.SIGTERM, _handle_exit)
signal.signal(signal.SIGINT,  _handle_exit)


def _analysis_budget_available(now: float) -> bool:
    global _analysis_window_start, _analysis_window_count
    if now - _analysis_window_start >= ANALYSIS_WINDOW_SECONDS:
        _analysis_window_start = now
        _analysis_window_count = 0
    if _analysis_window_count >= MAX_ANALYSES_PER_WINDOW:
        return False
    _analysis_window_count += 1
    return True


async def bar_handler(bar):
    if _shutdown:
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

    # Rate limit — one analysis per symbol per minute
    if now - _last_analyzed[bar.symbol] < MIN_ANALYSIS_GAP:
        return

    # Circuit breaker
    if now < _backoff_until[bar.symbol]:
        remaining = int(_backoff_until[bar.symbol] - now)
        print(f"  [{bar.symbol}] Circuit open — {remaining}s remaining")
        return

    if not _analysis_budget_available(now):
        print(f"  [{bar.symbol}] Analysis budget full — skipping this minute")
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
    global _stream_ref, _known_syms

    # Load symbols from DB (may include LLM-added ones since last restart)
    try:
        db_syms = [s.strip().upper() for s in get_setting("symbols").split(",") if s.strip()]
        active = db_syms if db_syms else SYMBOLS
    except Exception:
        active = SYMBOLS

    _known_syms = set(active)
    stream = StockDataStream(API_KEY, SECRET_KEY)
    _stream_ref = stream
    stream.subscribe_bars(bar_handler, *active)
    print(f"[ingest] Stream connected | symbols: {active}")

    # Start symbol watcher to pick up LLM-added symbols dynamically
    t = threading.Thread(target=_symbol_watcher, daemon=True)
    t.start()

    stream.run()


def _symbol_watcher():
    """Poll DB every 60s for new symbols and subscribe them to the live stream."""
    global _known_syms
    while not _shutdown:
        time.sleep(60)
        if _stream_ref is None:
            continue
        try:
            db_syms = {s.strip().upper() for s in get_setting("symbols").split(",") if s.strip()}
            new = db_syms - _known_syms
            if new:
                print(f"[ingest] New symbols detected: {new} — subscribing + backfilling")
                _stream_ref.subscribe_bars(bar_handler, *new)
                _known_syms |= new
                try:
                    from ingest.historical import ingest_historical
                    ingest_historical(symbols_override=list(new))
                except Exception as be:
                    print(f"[ingest] Backfill error for {new}: {be}")
        except Exception as e:
            print(f"[ingest] Symbol watcher error: {e}")


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
