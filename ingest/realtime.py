"""
Real-time market data ingestion via Alpaca WebSocket.
Streams 1-minute bars for configured symbols and writes to PostgreSQL.
Triggers LLM analysis after each new bar.
"""
import os, sys, json, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from alpaca.data.live import StockDataStream
from db.models import init_db, Session, Bar

API_KEY    = os.getenv("ALPACA_API_KEY")
SECRET_KEY = os.getenv("ALPACA_SECRET_KEY")
SYMBOLS    = [s.strip() for s in os.getenv("SYMBOLS", "AAPL,TSLA,SPY,QQQ").split(",")]


async def bar_handler(bar):
    session = Session()
    row = Bar(symbol=bar.symbol, ts=bar.timestamp,
              open=bar.open, high=bar.high, low=bar.low,
              close=bar.close, volume=bar.volume)
    session.merge(row)
    session.commit()
    session.close()
    print(f"[{bar.symbol}] {bar.timestamp}  C={bar.close:.2f}  V={int(bar.volume)}")

    # Trigger analysis after each bar (imported here to avoid circular deps)
    try:
        from analyze.analyze import run_analysis
        run_analysis(bar.symbol)
    except Exception as e:
        print(f"  Analysis error: {e}")


def main():
    print("Starting historical backfill...")
    from ingest.historical import ingest_historical
    ingest_historical()

    print(f"Starting real-time stream for {SYMBOLS}...")
    init_db()
    stream = StockDataStream(API_KEY, SECRET_KEY)
    stream.subscribe_bars(bar_handler, *SYMBOLS)
    stream.run()


if __name__ == "__main__":
    main()
