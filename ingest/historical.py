"""
Pull historical OHLCV bars via yfinance (free, no paid subscription needed).
Run once on startup to backfill the last N days for all configured symbols.

Resolution strategy (yfinance free tier limits):
  - Last 7 days   → 1-minute bars  (matches live WebSocket stream resolution)
  - Days 7–N      → 5-minute bars  (max 60 days available free)
"""
import os, sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy.dialects.postgresql import insert as pg_insert
from db.models import init_db, Session, Bar

SYMBOLS   = [s.strip() for s in os.getenv("SYMBOLS", "TQQQ,SQQQ,UPRO,SPXU,SOXL,SOXS,QQQ,SPY,SOXX").split(",")]
DAYS_BACK = int(os.getenv("HISTORY_DAYS", "30"))


def _upsert_df(session, symbol: str, df) -> int:
    """Insert bars from a yfinance DataFrame; silently skip duplicates."""
    if df is None or df.empty:
        return 0
    rows = []
    for ts, row in df.iterrows():
        # Normalize to UTC-naive datetime for consistent DB storage
        if hasattr(ts, "tzinfo") and ts.tzinfo is not None:
            ts_utc = ts.tz_convert("UTC").replace(tzinfo=None)
        else:
            ts_utc = ts.to_pydatetime().replace(tzinfo=None)
        rows.append({
            "symbol": symbol,
            "ts":     ts_utc,
            "open":   float(row["Open"]),
            "high":   float(row["High"]),
            "low":    float(row["Low"]),
            "close":  float(row["Close"]),
            "volume": float(row["Volume"]),
        })
    if not rows:
        return 0
    stmt = pg_insert(Bar.__table__).values(rows).on_conflict_do_nothing(
        index_elements=["symbol", "ts"]
    )
    result = session.execute(stmt)
    return result.rowcount or len(rows)


def ingest_historical(symbols_override: list = None):
    try:
        import yfinance as yf
    except ImportError:
        print("[historical] yfinance not installed — run: pip install yfinance")
        return

    init_db()
    symbols  = symbols_override or SYMBOLS
    now      = datetime.now(timezone.utc)
    end      = now
    start_1m = end - timedelta(days=7)
    start_5m = end - timedelta(days=min(DAYS_BACK, 59))   # yfinance 5m limit: 60 days

    print(f"[historical] Backfilling {DAYS_BACK}d history for {len(symbols)} symbols "
          f"(1m: last 7d, 5m: 7–{DAYS_BACK}d)...")

    session = Session()
    total   = 0

    for sym in symbols:
        try:
            ticker = yf.Ticker(sym)
            c_1m = c_5m = 0

            # 1-minute bars — most recent 7 days
            df_1m = ticker.history(start=start_1m.date(), end=end.date() + timedelta(days=1),
                                   interval="1m", auto_adjust=True, prepost=False)
            c_1m = _upsert_df(session, sym, df_1m)

            # 5-minute bars — older history (days 7 to DAYS_BACK)
            if DAYS_BACK > 7:
                df_5m = ticker.history(start=start_5m.date(), end=start_1m.date(),
                                       interval="5m", auto_adjust=True, prepost=False)
                c_5m = _upsert_df(session, sym, df_5m)

            session.commit()
            total += c_1m + c_5m
            print(f"  [{sym}] stored {c_1m + c_5m} bars  (1m={c_1m}, 5m={c_5m})")

        except Exception as e:
            print(f"  [{sym}] Error: {e}")
            session.rollback()

    session.close()
    print(f"[historical] Done — {total:,} bars stored across {len(SYMBOLS)} symbols.")


if __name__ == "__main__":
    ingest_historical()
