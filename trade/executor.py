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
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo
sys.path.insert(0, str(Path(__file__).parent.parent))

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest, ClosePositionRequest
from alpaca.trading.enums import OrderSide, TimeInForce
from db.models import Session, Signal, Trade, Bar, get_setting

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
    today_start = datetime.combine(date.today(), dtime.min)
    trades = (session.query(Trade)
              .filter(Trade.ts >= today_start,
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
            if is_crypto(pos.symbol) and reason.startswith("EOD"):
                print(f"  [executor] {reason}: skipping crypto {pos.symbol} (24/7 asset)")
                continue
            client.close_position(pos.symbol)
            print(f"  [executor] {reason}: closed {pos.symbol} ({pos.qty} shares)")
    except Exception as e:
        print(f"  [executor] {reason} close error: {e}")


def eod_check():
    """Call this on every bar tick — closes all positions at EOD cutoff."""
    now = datetime.now(ET).time()
    if now >= EOD_CLOSE_TIME:
        close_all_positions("EOD-15:45")


# ── Crypto helpers ─────────────────────────────────────────────────────────────

CRYPTO_BASES = {"BTC", "ETH", "SOL", "AVAX", "MATIC", "DOT", "ADA", "LINK",
                "UNI", "DOGE", "SHIB", "LTC", "XRP", "ATOM", "NEAR", "APT"}


def is_crypto(symbol: str) -> bool:
    """True for crypto symbols in both BTC/USD (stream) and BTCUSD (position) formats."""
    if "/" in symbol:
        return True
    upper = symbol.upper()
    return any(upper.startswith(base) for base in CRYPTO_BASES)


def _get_crypto_position_qty(symbol: str) -> float:
    """Return available quantity for a crypto asset (Coinbase primary, Alpaca fallback)."""
    from trade.coinbase_client import is_configured as cb_ok, get_crypto_balance
    if cb_ok():
        return get_crypto_balance(symbol.split("/")[0])
    alpaca_sym = symbol.replace("/", "")
    try:
        pos = get_client().get_open_position(alpaca_sym)
        return float(pos.qty)
    except Exception:
        return 0.0


# ── Options trading ────────────────────────────────────────────────────────────

def get_option_chain(underlying: str, direction: str, dte_max: int = 14) -> list:
    """
    Fetch active option contracts from Alpaca for the given underlying and direction.
    Returns a list of contract objects, or [] on error.
    """
    try:
        from alpaca.trading.requests import GetOptionContractsRequest
        from alpaca.trading.enums import ContractType
        client        = get_client()
        contract_type = ContractType.CALL if direction.lower() == "call" else ContractType.PUT
        today         = date.today()
        exp_min       = str(today + timedelta(days=1))          # at least 1 business day out
        exp_max       = str(today + timedelta(days=dte_max))
        contracts = client.get_option_contracts(GetOptionContractsRequest(
            underlying_symbols=[underlying],
            status="active",
            expiration_date_gte=exp_min,
            expiration_date_lte=exp_max,
            contract_type=contract_type,
            limit=50,
        ))
        return list(contracts) if contracts else []
    except Exception as e:
        print(f"  [executor] get_option_chain({underlying} {direction}): {e}")
        return []


def pick_option_contract(contracts: list, current_price: float,
                         moneyness: str, direction: str):
    """
    Select the best contract from a chain by targeting a strike near the given
    moneyness, preferring contracts that have a recent close_price.
    """
    if not contracts:
        return None

    # Map moneyness → target strike multiplier
    multipliers = {
        "atm":       1.00,
        "slight_otm": 1.02 if direction == "call" else 0.98,
        "deep_otm":   1.05 if direction == "call" else 0.95,
        "itm":        0.97 if direction == "call" else 1.03,
    }
    target_strike = current_price * multipliers.get(moneyness, 1.00)

    def dist(c):
        try:
            return abs(float(c.strike_price or 0) - target_strike)
        except Exception:
            return 99999.0

    contracts.sort(key=dist)

    # Return the first contract with a usable close_price; fall back to closest
    for c in contracts[:5]:
        try:
            if c.close_price and float(c.close_price) > 0:
                return c
        except Exception:
            pass
    return contracts[0]


def execute_option_order(underlying: str, option_rec: dict,
                         signal_id: int, mode: str):
    """
    Fetch option chain, select the best contract, and execute (or suggest) an order.

    option_rec fields: direction, moneyness, dte_preference, reasoning
    """
    direction  = str(option_rec.get("direction", "call")).lower()
    moneyness  = str(option_rec.get("moneyness", "atm"))
    dte_pref   = int(option_rec.get("dte_preference", 7))
    reasoning  = str(option_rec.get("reasoning", ""))[:120]

    # Current price of the underlying
    db  = Session()
    bar = db.query(Bar).filter(Bar.symbol == underlying).order_by(Bar.ts.desc()).first()
    db.close()
    if not bar:
        print(f"  [executor] No price data for {underlying} — option order skipped")
        return
    current_price = float(bar.close)

    # Fetch chain and pick contract
    contracts = get_option_chain(underlying, direction, dte_max=max(dte_pref + 3, 14))
    contract  = pick_option_contract(contracts, current_price, moneyness, direction)
    if not contract:
        print(f"  [executor] No suitable option contract for {underlying} {direction}")
        return

    close_price    = float(contract.close_price or 0)
    premium_per_ct = close_price * 100      # 1 contract = 100 shares
    if premium_per_ct <= 0:
        print(f"  [executor] Option {contract.symbol}: no price data — skipping")
        return

    max_premium = float(get_setting("max_option_premium_usd", "200"))
    if premium_per_ct > max_premium:
        print(f"  [executor] Option premium ${premium_per_ct:.0f} > limit ${max_premium:.0f} — skipping")
        return

    qty = max(1, int(max_premium / premium_per_ct))
    print(f"  [executor] Option: {direction.upper()} {contract.symbol} "
          f"strike=${contract.strike_price} exp={contract.expiration_date} "
          f"x{qty} @ ~${close_price:.2f}/sh | {reasoning}")

    db = Session()
    try:
        trade = Trade(
            symbol=str(contract.symbol)[:10],
            side="buy",
            qty=qty,
            price=close_price,
            mode=mode,
            signal_id=signal_id,
            status="suggested",
            option_contract=str(contract.symbol)[:30],
            option_expiry=str(contract.expiration_date)[:12],
        )

        if mode == "suggest":
            db.add(trade)
            db.commit()
            return

        # Paper / live execution
        try:
            order = get_client().submit_order(MarketOrderRequest(
                symbol=str(contract.symbol),
                qty=qty,
                side=OrderSide.BUY,
                time_in_force=TimeInForce.DAY,
            ))
            trade.alpaca_id = str(order.id)
            trade.status    = "submitted"
            print(f"  [executor] Option order submitted: {order.id}")
        except Exception as e:
            trade.status = "rejected"
            print(f"  [executor] Option order rejected: {e}")

        db.add(trade)
        db.commit()
    finally:
        db.close()


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

    # Watchlist-only mode: skip symbols not in the current stock watchlist
    if get_setting("watchlist_only", "true") == "true":
        wl = [s.strip().upper() for s in get_setting("symbols", "").split(",") if s.strip()]
        if wl and sig.symbol.upper() not in wl:
            print(f"  [executor] SKIP — {sig.symbol} not in watchlist (watchlist_only mode)")
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
        action_str = sig.action.upper()
        sym_str    = sig.symbol
        session.commit()
        session.close()
        print(f"  [executor] Suggestion: {action_str} {qty}x {sym_str} @ ~${price:.2f}")
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


def execute_crypto_signal(signal_id: int):
    """
    Execute a crypto signal via Coinbase Advanced Trade (primary) or Alpaca (fallback).
    Buys use USD notional; sells use the full available base-currency balance.
    """
    session = Session()
    sig = session.query(Signal).filter(Signal.id == signal_id).first()
    if not sig or sig.action == "hold":
        session.close()
        return

    # Read mode from DB (live/suggest) — falls back to env var if not set
    mode = get_setting("trade_mode", TRADE_MODE)
    print(f"  [executor] Crypto Mode={mode}  {sig.symbol} {sig.action.upper()}  conf={sig.confidence:.2f}")

    if sig.confidence < MIN_CONFIDENCE:
        print(f"  [executor] Below confidence threshold ({sig.confidence:.2f} < {MIN_CONFIDENCE}) — skip")
        session.close()
        return

    # Watchlist-only mode: skip symbols not in the current crypto watchlist
    if get_setting("watchlist_only", "true") == "true":
        crypto_wl = [s.strip().upper() for s in get_setting("crypto_symbols", "").split(",") if s.strip()]
        if crypto_wl and sig.symbol.upper() not in crypto_wl:
            print(f"  [executor] SKIP — {sig.symbol} not in crypto watchlist (watchlist_only mode)")
            session.close()
            return

    indicators = json.loads(sig.indicators or "{}")
    price      = indicators.get("close", 1.0)

    trade = Trade(
        symbol=sig.symbol, side=sig.action, qty=0, price=price,
        mode=mode, signal_id=sig.id, status="suggested",
    )

    if mode == "suggest":
        session.add(trade)
        sig.acted_on = True
        action_str = sig.action.upper()
        sym_str    = sig.symbol
        session.commit()
        session.close()
        print(f"  [executor] Crypto suggestion: {action_str} {sym_str} @ ~${price:.2f}")
        return

    # Safety: daily loss halt
    pnl = daily_pnl()
    if pnl <= -MAX_DAILY_LOSS:
        print(f"  [executor] HALT — daily loss limit hit (${pnl:.2f}). Closing all positions.")
        close_all_positions("LOSS-HALT")
        session.close()
        return

    from trade.coinbase_client import (
        is_configured as cb_ok, coinbase_symbol,
        place_market_buy, place_market_sell, get_crypto_balance,
    )

    # Determine buy size: respect DB max_position_usd AND available balance
    max_pos_db = float(get_setting("max_position_usd", str(MAX_POSITION_USD)))

    try:
        if cb_ok():
            # ── Coinbase execution ──────────────────────────────────────────
            cb_sym = coinbase_symbol(sig.symbol)
            if sig.action == "buy":
                # Use min of configured max and actual available USD+USDC balance
                available_usd  = get_crypto_balance("USD") + get_crypto_balance("USDC")
                buy_size       = round(min(max_pos_db, available_usd) * 0.99, 2)  # 1% fee buffer
                if buy_size < 1.0:
                    print(f"  [executor] Insufficient balance (${available_usd:.2f}) — buy skipped")
                    session.close()
                    return
                resp      = place_market_buy(cb_sym, buy_size)
                trade.qty = round(buy_size / price, 6) if price > 0 else 0
                print(f"  [executor] Buying ${buy_size:.2f} of {cb_sym} (balance: ${available_usd:.2f}, max: ${max_pos_db:.2f})")
            else:
                base = sig.symbol.split("/")[0]
                qty  = get_crypto_balance(base)
                if qty <= 0:
                    print(f"  [executor] No Coinbase balance for {base} — sell skipped")
                    session.close()
                    return
                resp      = place_market_sell(cb_sym, qty)
                trade.qty = qty
            # Coinbase wraps the order ID under success_response in some SDK versions
            order_id       = (resp.get("order_id")
                              or resp.get("success_response", {}).get("order_id", "?"))
            trade.alpaca_id = str(order_id)[:50]
            trade.status    = "submitted"
            print(f"  [executor] Coinbase {sig.action.upper()} {cb_sym} → {order_id}")
        else:
            # ── Alpaca fallback ─────────────────────────────────────────────
            client = get_client()
            if sig.action == "buy":
                order = client.submit_order(MarketOrderRequest(
                    symbol=sig.symbol,
                    notional=MAX_POSITION_USD,
                    side=OrderSide.BUY,
                    time_in_force=TimeInForce.IOC,
                ))
                trade.qty = round(MAX_POSITION_USD / price, 6) if price > 0 else 0
            else:
                qty = _get_crypto_position_qty(sig.symbol)
                if qty <= 0:
                    print(f"  [executor] No open position for {sig.symbol} — sell skipped")
                    session.close()
                    return
                order = client.submit_order(MarketOrderRequest(
                    symbol=sig.symbol,
                    qty=qty,
                    side=OrderSide.SELL,
                    time_in_force=TimeInForce.IOC,
                ))
                trade.qty = qty
            trade.alpaca_id = str(order.id)
            trade.status    = "submitted"
            print(f"  [executor] Alpaca crypto {sig.action.upper()} {sig.symbol} → {order.id}")
    except Exception as e:
        trade.status = "rejected"
        print(f"  [executor] Crypto order rejected: {e}")

    session.add(trade)
    sig.acted_on = True
    session.commit()
    session.close()
