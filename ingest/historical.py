"""
Pull historical OHLCV bars from Alpaca and store in PostgreSQL.
Run once on startup to backfill the last N days for all configured symbols.
"""
import os, sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame
from db.models import init_db, Session, Bar

API_KEY    = os.getenv("ALPACA_API_KEY")
SECRET_KEY = os.getenv("ALPACA_SECRET_KEY")
SYMBOLS    = [s.strip() for s in os.getenv("SYMBOLS", "AAPL,TSLA,SPY,QQQ").split(",")]
DAYS_BACK  = int(os.getenv("HISTORY_DAYS", "30"))


def ingest_historical():
    init_db()
    client = StockHistoricalDataClient(API_KEY, SECRET_KEY)
    end   = datetime.now(timezone.utc)
    start = end - timedelta(days=DAYS_BACK)

    print(f"Fetching {DAYS_BACK} days of history for {SYMBOLS}...")
    req = StockBarsRequest(symbol_or_symbols=SYMBOLS, timeframe=TimeFrame.Minute,
                           start=start, end=end)
    bars = client.get_stock_bars(req)

    session = Session()
    count = 0
    for symbol, bar_list in bars.items():
        for b in bar_list:
            row = Bar(symbol=symbol, ts=b.timestamp,
                      open=b.open, high=b.high, low=b.low,
                      close=b.close, volume=b.volume)
            session.merge(row)
            count += 1
    session.commit()
    session.close()
    print(f"Stored {count} bars.")


if __name__ == "__main__":
    ingest_historical()
