"""
Trade execution layer with three modes:
  suggest — log signal only, no order submitted
  paper   — submit to Alpaca paper trading account
  live    — submit to Alpaca live account with hard safety guards

PDT NOTE: As of June 4, 2026, the SEC eliminated the pattern day trader (PDT)
designation and $25,000 minimum equity requirement. Brokerages may still apply
their own intraday margin rules during the transition period (compliance deadline
Oct 20, 2027). Verify your broker's current policy before enabling live mode.
"""
import os, sys, json
from datetime import date, datetime, time as dtime
from pathlib import Path
from zoneinfo import ZoneInfo
sys.path.insert(0, str(Path(__file__).parent.parent))

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest, ClosePositionRequest
from alpaca.trading.enums import OrderSide, TimeInForce
from db.models import Session, Signal, Trade

TRADE_MODE       = os.getenv("TRADE_MODE", "suggest")
ALPACA_PAPER     = os.getenv("ALPACA_PAPER", "true").lower() == "true"
API_KEY          = os.getenv("ALPACA_API_KEY")
SECRET_KEY       = os.getenv("ALPACA_SECRET_KEY")
MAX_POSITION_USD = float(os.getenv("MAX_POSITION_USD", "500"))
MAX_DAILY_LOSS   = float(os.getenv("MAX_DAILY_LOSS_USD", "200"))
MAX_OPEN         = int(os.getenv("MAX_OPEN_POSITIONS", "5"))
MIN_CONFIDENCE   = 0.65
ET               = ZoneInfo("America/New_York")
EOD_CLOSE_TIME   = dtime(15, 45)  # force-close all positions at 3:45 PM ET


def get_client() -> TradingClient:
    return TradingClient(API_KEY, SECRET_KEY, paper=ALPACA_PAPER)


def daily_pnl() -> float:
    """Net P&L from today's trades (negative = loss)."""
    session = Session()
    today = date.today().isoformat()
    trades = (session.query(Trade)
              .filter(Trade.ts.cast(str).startswith(today),
                      Trade.status == "filled")
              .all())
    session.close()
    bought = sum(t.price * t.qty for t in trades if t.side == "buy")
    sold   = sum(t.price * t.qty for t in trades if t.side == "sell")
    return sold - bought


def close_all_positions(reason: str = "EOD"):
    """Force-close all open positions. Called at 3:45 PM ET or on daily loss halt."""
    if TRADE_MODE == "suggest":
        print(f"  [executor] {reason} close skipped — suggest mode")
        return
    try:
        client = get_client()
        positions = client.get_all_positions()
        if not positions:
            print(f"  [executor] {reason}: no open positions to close")
            return
        for pos in positions:
            client.close_position(pos.symbol)
            print(f"  [executor] {reason}: closed {pos.symbol} ({pos.qty} shares)")
    except Exception as e:
        print(f"  [executor] {reason} close error: {e}")


def eod_check():
    """Call this on every bar tick — closes all positions at EOD cutoff."""
    now = datetime.now(ET).time()
    if now >= EOD_CLOSE_TIME:
        close_all_positions("EOD-15:45")


def execute_signal(signal_id: int):
    eod_check()

    session = Session()
    sig = session.query(Signal).filter(Signal.id == signal_id).first()
    if not sig or sig.action == "hold":
        session.close()
        return

    mode = TRADE_MODE
    print(f"  [executor] Mode={mode}  {sig.symbol} {sig.action.upper()}  conf={sig.confidence:.2f}")

    if sig.confidence < MIN_CONFIDENCE:
        print(f"  [executor] Below confidence threshold ({sig.confidence:.2f} < {MIN_CONFIDENCE}) — skip")
        session.close()
        return

    # Leveraged ETFs: reduce position size vs regular stocks due to amplified moves
    indicators = json.loads(sig.indicators or "{}")
    price      = indicators.get("close", 1.0)
    atr        = indicators.get("atr_14", price * 0.01)
    # Size by ATR: risk no more than 2% of MAX_POSITION_USD per ATR unit
    qty = max(1, int((MAX_POSITION_USD * 0.02) / atr)) if atr > 0 else max(1, int(MAX_POSITION_USD / price))

    trade = Trade(
        symbol=sig.symbol, side=sig.action, qty=qty, price=price,
        mode=mode, signal_id=sig.id, status="suggested",
    )

    if mode == "suggest":
        session.add(trade)
        sig.acted_on = True
        session.commit()
        session.close()
        print(f"  [executor] Suggestion: {sig.action.upper()} {qty}x {sig.symbol} @ ~${price:.2f}")
        return

    # Safety guards for paper/live
    pnl = daily_pnl()
    if pnl <= -MAX_DAILY_LOSS:
        print(f"  [executor] HALT — daily loss limit hit (${pnl:.2f}). Closing all positions.")
        close_all_positions("LOSS-HALT")
        session.close()
        return

    client = get_client()
    open_positions = len(client.get_all_positions())
    if open_positions >= MAX_OPEN and sig.action == "buy":
        print(f"  [executor] SKIP — max open positions ({MAX_OPEN}) reached")
        session.close()
        return

    try:
        order = client.submit_order(MarketOrderRequest(
            symbol=sig.symbol,
            qty=qty,
            side=OrderSide.BUY if sig.action == "buy" else OrderSide.SELL,
            time_in_force=TimeInForce.DAY,
        ))
        trade.alpaca_id = str(order.id)
        trade.status    = "submitted"
        print(f"  [executor] Order submitted: {order.id}  {sig.action.upper()} {qty}x {sig.symbol}")
    except Exception as e:
        trade.status = "rejected"
        print(f"  [executor] Order rejected: {e}")

    session.add(trade)
    sig.acted_on = True
    session.commit()
    session.close()
