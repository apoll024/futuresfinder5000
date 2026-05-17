"""
EOD Settlement — runs daily at 16:15 ET (Monday–Friday).

For every acted-on buy/sell signal from today that has not yet been settled,
looks up the final bar close price and calculates outcome_pct and was_correct.

This labeled dataset is the foundation for training the XGBoost model that will
eventually replace the LLM as the primary signal generator.

Learning loop:
  Signal generated → acted_on=True → EOD settlement → outcome_pct / was_correct
  → weekly XGBoost training run → model replaces LLM for signal generation
"""
import os, sys, time
from datetime import datetime, date, time as dtime
from pathlib import Path
from zoneinfo import ZoneInfo
sys.path.insert(0, str(Path(__file__).parent.parent))

from db.models import Session, Signal, Bar, init_db

ET          = ZoneInfo("America/New_York")
SETTLE_TIME = dtime(16, 15)   # 15 min after market close — all final bars should be recorded


def get_final_price(symbol: str, trade_date: date) -> float | None:
    """Return the last 1-min bar close for symbol on trade_date."""
    session = Session()
    bar = (session.query(Bar)
           .filter(
               Bar.symbol == symbol,
               Bar.ts >= datetime(trade_date.year, trade_date.month, trade_date.day, 9, 30),
               Bar.ts <  datetime(trade_date.year, trade_date.month, trade_date.day, 16, 1),
           )
           .order_by(Bar.ts.desc())
           .first())
    session.close()
    return round(float(bar.close), 4) if bar else None


def settle_day(trade_date: date):
    """Settle all acted-on, unsettled buy/sell signals for trade_date."""
    day_start = datetime(trade_date.year, trade_date.month, trade_date.day, 0, 0)
    day_end   = datetime(trade_date.year, trade_date.month, trade_date.day, 23, 59)

    session = Session()
    signals = (session.query(Signal)
               .filter(
                   Signal.acted_on == True,
                   Signal.settled  == False,
                   Signal.action.in_(["buy", "sell"]),
                   Signal.ts >= day_start,
                   Signal.ts <= day_end,
               )
               .all())

    print(f"[settler] Settling {len(signals)} signal(s) for {trade_date}")
    settled_count = 0

    for sig in signals:
        if sig.entry_price is None:
            print(f"  [settler] Signal {sig.id} ({sig.symbol}) has no entry_price — skip")
            continue

        exit_price = get_final_price(sig.symbol, trade_date)
        if exit_price is None:
            print(f"  [settler] No bar data for {sig.symbol} on {trade_date} — skip")
            continue

        # P&L from the perspective of the signal action
        if sig.action == "buy":
            outcome_pct = (exit_price - sig.entry_price) / sig.entry_price * 100
        else:   # sell / short
            outcome_pct = (sig.entry_price - exit_price) / sig.entry_price * 100

        sig.exit_price  = exit_price
        sig.exit_time   = datetime.now(ET).replace(tzinfo=None)
        sig.outcome_pct = round(outcome_pct, 4)
        sig.was_correct = outcome_pct > 0
        sig.settled     = True
        settled_count  += 1

        icon = "✓" if sig.was_correct else "✗"
        print(f"  [settler] {sig.symbol} {sig.action.upper()} | "
              f"entry={sig.entry_price:.2f} exit={exit_price:.2f} "
              f"outcome={outcome_pct:+.2f}% {icon}")

    session.commit()
    session.close()

    total = len(signals)
    win_rate = (settled_count and
                sum(1 for s in signals if s.was_correct) / settled_count * 100)
    print(f"[settler] Done — {settled_count}/{total} settled | "
          f"day win rate: {win_rate:.1f}%")


def wait_until(target: dtime):
    now = datetime.now(ET)
    target_dt = now.replace(hour=target.hour, minute=target.minute, second=0, microsecond=0)
    delta = (target_dt - now).total_seconds()
    if delta > 0:
        print(f"[settler] Waiting {delta/3600:.1f}h until {target} ET...")
        time.sleep(delta)


def run():
    print("[settler] EOD Settlement service started")
    init_db()
    while True:
        wait_until(SETTLE_TIME)
        today = datetime.now(ET).date()
        if today.weekday() < 5:   # Mon–Fri only
            settle_day(today)
        else:
            print(f"[settler] {today} is a weekend — skipping")
        # Sleep 23h before next check (avoids double-trigger within same day)
        time.sleep(23 * 3600)


if __name__ == "__main__":
    run()
