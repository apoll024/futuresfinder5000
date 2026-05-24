"""
Real-time crypto data ingestion via Alpaca WebSocket.
Streams 1-minute bars for crypto pairs, writes to PostgreSQL, triggers LLM analysis.

Resilience:
  - Exponential backoff reconnect on WebSocket disconnect (5s → 120s)
  - Per-symbol circuit breaker: backs off 15 min after 3 consecutive LLM failures
  - Rate limit: one analysis per symbol per 55 seconds (debounce rapid bars)
  - Graceful shutdown on SIGTERM / SIGINT
  - Crypto is 24/7 — no market-hours gate applied
"""
import os, sys, time, signal, threading
from collections import defaultdict
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from alpaca.data.live import CryptoDataStream
from db.models import init_db, Session, Bar, get_setting

API_KEY    = os.getenv("ALPACA_API_KEY")
SECRET_KEY = os.getenv("ALPACA_SECRET_KEY")

# Circuit breaker state per symbol
_fail_counts:   dict = defaultdict(int)
_backoff_until: dict = defaultdict(float)
_last_analyzed: dict = defaultdict(float)

CIRCUIT_OPEN_AFTER = 3
BACKOFF_SECONDS    = 900    # 15 min
MIN_ANALYSIS_GAP   = 55     # seconds between analyses per symbol

_shutdown    = False
_stream_ref  = None
_known_syms: set = set()


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


def run_stream():
    global _stream_ref, _known_syms

    active = _get_active_symbols()
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
            db_syms = set(_get_active_symbols())
            new = db_syms - _known_syms
            if new:
                print(f"[crypto] New symbols detected: {new} — subscribing")
                _stream_ref.subscribe_bars(bar_handler, *new)
                _known_syms |= new
        except Exception as e:
            print(f"[crypto] Symbol watcher error: {e}")


def main():
    init_db()

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
